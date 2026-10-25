import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from controller.schemas.contracts import Patch, SandboxRef
from improvements.patches import inspect_diff, prepare, git, structural_checks
from improvements.lifecycle import (Approval, DeploymentContext, ReplayEvidence, TestReceipt as Receipt,
                                    approve, deploy, compare)

DIFF = b'''diff --git a/src/checkout/main.go b/src/checkout/main.go
--- a/src/checkout/main.go
+++ b/src/checkout/main.go
@@ -1 +1 @@
-old
+new
'''
BASE = 'a' * 40
DIGEST = 'b' * 64
SANDBOX = SandboxRef(docker_context='dedicated', daemon_id='daemon-approved', compose_project='faultline-sandbox')
PATCH = Patch(patch_id='patch-test', base_revision=BASE, diff_sha256=DIGEST,
              affected_files=['src/checkout/main.go'], rationale='Test exact revision approval',
              image_digests={'checkout':'demo@sha256:'+'c'*64},
              rollback_images={'checkout':'demo@sha256:'+'d'*64})
RECEIPT = Receipt(BASE, DIGEST, ('application-tests',), True, 'e'*64)
CONTEXT = DeploymentContext(SANDBOX,True,True,False,True,True)


@pytest.mark.parametrize('path',['../escape','src/payment/../../escape','config/sandbox.yaml',
                                'src/frontend/a\\b','/src/payment/a'])
def test_patch_path_refused(path):
    """Diffs touching paths outside the application sources are refused."""
    with pytest.raises(ValueError):
        inspect_diff(DIFF.replace(b'src/checkout/main.go',path.encode()))


@pytest.mark.parametrize('extra',[b'new file mode 120000\n',b'old mode 100644\n',
                                 b'GIT binary patch\n',b'rename from src/payment/a\n'])
def test_non_text_edits_refused(extra):
    """Renames, copies, binary patches and mode changes are refused."""
    with pytest.raises(ValueError): inspect_diff(DIFF+extra)


def test_header_mismatch_refused():
    """A diff whose old and new paths differ is refused."""
    with pytest.raises(ValueError): inspect_diff(DIFF.replace(b'+++ b/src/checkout/main.go',b'+++ b/config/sandbox.yaml'))


def test_isolated_prepare_preserves_source(tmp_path):
    """Preparing a patch works in an isolated worktree and leaves the source checkout unchanged."""
    source=tmp_path/'source';source.mkdir()
    git(source,'init');git(source,'config','user.name','Test');git(source,'config','user.email','test@example.invalid')
    (source/'src/checkout').mkdir(parents=True);(source/'src/checkout/main.go').write_text('old\n')
    git(source,'add','.');git(source,'commit','-m','fixture');base=git(source,'rev-parse','HEAD')
    workspace=tmp_path/'prepared'
    patch=prepare(source,workspace,DIFF,'patch-isolated',base,'Fixture preparation')
    assert patch.affected_files==['src/checkout/main.go']
    assert (workspace/'src/checkout/main.go').read_text()=='new\n'
    assert (source/'src/checkout/main.go').read_text()=='old\n'
    assert git(source,'status','--porcelain')==''
    assert structural_checks(workspace,patch)['source_matches_review']
    (workspace/'src/checkout/main.go').write_text('unreviewed\n')
    with pytest.raises(ValueError,match='source changed'): structural_checks(workspace,patch)
    with pytest.raises(ValueError): prepare(source,workspace,DIFF,'patch-isolated',base,'Duplicate')


def test_symlink_edit_refused(tmp_path):
    """Patches that edit a symlink are refused."""
    source=tmp_path/'source';source.mkdir();git(source,'init')
    git(source,'config','user.name','Test');git(source,'config','user.email','test@example.invalid')
    (source/'src/checkout').mkdir(parents=True);(source/'src/checkout/main.go').symlink_to('../../../external')
    git(source,'add','.');git(source,'commit','-m','symlink');base=git(source,'rev-parse','HEAD')
    with pytest.raises(ValueError): prepare(source,tmp_path/'workspace',DIFF,'patch-link',base,'Refuse')


@pytest.mark.parametrize('change',[{'diff_sha256':'f'*64},{'base_revision':'f'*40},
                                 {'image_digests':{'checkout':'demo@sha256:'+'f'*64}},
                                 {'rollback_images':{'checkout':'demo@sha256:'+'f'*64}}])
def test_changed_revision_invalidates_approval(change):
    """Changing the patch's base or diff after approval invalidates the approval."""
    approval=approve(PATCH,RECEIPT,SANDBOX,'Reviewer')
    with pytest.raises(ValueError): deploy(PATCH.model_copy(update=change),RECEIPT,approval,CONTEXT)


def test_test_or_target_change_invalidates_approval():
    """Changing the test receipt or the target sandbox invalidates the approval."""
    approval=approve(PATCH,RECEIPT,SANDBOX,'Reviewer')
    with pytest.raises(ValueError): deploy(PATCH,replace(RECEIPT,log_sha256='f'*64),approval,CONTEXT)
    target=SANDBOX.model_copy(update={'daemon_id':'different'})
    with pytest.raises(ValueError): deploy(PATCH,RECEIPT,approval,replace(CONTEXT,sandbox=target))


@pytest.mark.parametrize('tests',[replace(RECEIPT,passed=False),replace(RECEIPT,checks=('whitespace',))])
def test_structural_or_failed_tests_cannot_approve(tests):
    """Missing, structural-only or failed tests cannot approve a patch."""
    with pytest.raises(ValueError): approve(PATCH,tests,SANDBOX,'Reviewer')


@pytest.mark.parametrize('field',['isolation_approved','backend_gates_passed','cleanup_verified','recovery_verified'])
def test_deployment_prerequisites(field):
    """Deployment is refused unless every prerequisite (isolation, gates, cleanup, recovery) holds."""
    approval=approve(PATCH,RECEIPT,SANDBOX,'Reviewer')
    with pytest.raises(ValueError): deploy(PATCH,RECEIPT,approval,replace(CONTEXT,**{field:False}))


def test_active_run_and_default_backend_block_deployment():
    """An active run or the default disabled backend blocks deployment."""
    approval=approve(PATCH,RECEIPT,SANDBOX,'Reviewer')
    with pytest.raises(ValueError): deploy(PATCH,RECEIPT,approval,replace(CONTEXT,active_run=True))
    with pytest.raises(ValueError,match='unavailable'): deploy(PATCH,RECEIPT,approval,CONTEXT)


def evidence(**changes):
    """Replay evidence of the original run, with optional changes."""
    values=dict(run_id='run-before',capability_id='payment-unreachable',parameters_digest='a'*64,
                workload_version='1',workload_digest='b'*64,policy_version='1',sandbox_digest='c'*64,
                source_revision=BASE,image_digests={'checkout':'old'},measured=True,fresh=True,
                complete=True,cleanup_verified=True,recovery_verified=True,
                payment_succeeded_during_outage=False,expected_category_observed=False,
                protected_paths_healthy=True,cart_retained=True,response_within_bound=True)
    values.update(changes)
    return ReplayEvidence(**values)


def replay(**changes):
    """Replay evidence after the patch, with optional changes."""
    return evidence(run_id='run-after',source_revision='f'*40,image_digests={'checkout':'new'},expected_category_observed=True,**changes)


def test_matched_replay_improves_error_without_succeeding_payment():
    """A matched replay with better error handling is conclusive and counts as improved."""
    result=compare(evidence(),replay())
    assert result['conclusive'] and result['outcome']=='improved'


@pytest.mark.parametrize('change',[{'workload_digest':'different'},{'parameters_digest':'different'},
                                 {'fresh':False},{'complete':False},{'recovery_verified':False},
                                 {'payment_succeeded_during_outage':True}])
def test_unmatched_or_unverified_replay_not_promoted(change):
    """A replay that does not match the original, or is unverified, is not promoted."""
    result=compare(evidence(),replay(**change))
    assert not result['conclusive'] and result['outcome']=='unverified'


def test_replay_regression_not_promoted():
    """A replay that loses the cart is marked ineffective."""
    assert compare(evidence(),replay(cart_retained=False))['outcome']=='ineffective'


def test_transaction_rolls_back_failed_deployment_and_blocks_failed_cleanup():
    """A failed deployment is rolled back, and if the rollback is also unhealthy new experiments are
    blocked.
    """
    from contextlib import nullcontext
    from improvements.transactions import TransactionalBackend
    class Operator:
        """Fake image operator with scripted health results."""
        def __init__(self,health):
            """Start from the rollback images with scripted health checks."""
            self.health=iter(health);self.images=dict(PATCH.rollback_images);self.blocked=False;self.changed=[]
        def exclusive_lock(self):
            """No-op lock."""
            return nullcontext()
        def verify_idle_recovered_sandbox(self,sandbox):
            """Check the expected sandbox is used."""
            assert sandbox==SANDBOX
        def current_images(self,services):
            """The current images."""
            return dict(self.images)
        def replace_images(self,images):
            """Record and apply an image change."""
            self.changed.append(dict(images));self.images=dict(images)
        def verify_health(self,services,timeout_seconds):
            """Next scripted health result."""
            return next(self.health)
        def block_new_experiments(self):
            """Record that experiments were blocked."""
            self.blocked=True
    for health,outcome,blocked in [([True],'verified',False),([False,True],'rolled_back',False),([False,False],'needs_attention',True)]:
        op=Operator(health);records=[];backend=TransactionalBackend(op,records.append)
        result=backend.deploy_transaction(PATCH.image_digests,PATCH.rollback_images,SANDBOX)
        assert result['outcome']==outcome and op.blocked==blocked
        assert records[0]['outcome']=='deploying'
        if outcome!='verified': assert op.images==PATCH.rollback_images


def test_transaction_revalidates_old_images_before_mutation():
    """A deployment whose current images differ from the expected rollback images is refused before
    any change.
    """
    from contextlib import nullcontext
    from improvements.transactions import TransactionalBackend
    class ChangedOperator:
        """Fake operator whose running images changed unexpectedly."""
        def exclusive_lock(self):
            """No-op lock."""
            return nullcontext()
        def verify_idle_recovered_sandbox(self,sandbox):
            """Accept the sandbox."""
            pass
        def current_images(self,services):
            """Images that differ from the expected rollback images."""
            return {'checkout':'different'}
        def replace_images(self,images):
            """Must never be called."""
            pytest.fail('must not mutate stale deployment')
    with pytest.raises(ValueError,match='no longer match'):
        TransactionalBackend(ChangedOperator(),lambda record:None).deploy_transaction(PATCH.image_digests,PATCH.rollback_images,SANDBOX)


def test_truthy_strings_do_not_authorize_actions_or_verify_replay():
    """Strings like 'false' are not treated as true for approvals or replay checks."""
    with pytest.raises(ValueError): approve(PATCH,replace(RECEIPT,passed='false'),SANDBOX,'Reviewer')
    approval=approve(PATCH,RECEIPT,SANDBOX,'Reviewer')
    with pytest.raises(ValueError): deploy(PATCH,RECEIPT,approval,replace(CONTEXT,isolation_approved='false'))
    assert compare(evidence(),replay(measured='false'))['outcome']=='unverified'
