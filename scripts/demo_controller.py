#!/usr/bin/env python3
"""Portable simulated lifecycle demonstration. Never targets Docker or the shop.

Uses the test sandbox against a temporary COPY of healthy flags, the real runner,
SQLite journal, independent watchdog, evidence exporter and free rule diagnoser.
This is integration evidence, never a live payment-fix acceptance result.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.agent.service import diagnose_run
from controller.executor.flag_store import FLAG_FILE
from controller.executor.runner import execute
from controller.report.build import build_report
from controller.store import RunStore
from tests.fake_env import FakeEnvironment, fast_limits, make_plan


def main():
    """Run a fully simulated controller demo in a temp directory (no Docker) and save its result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT/'artifacts/results/simulated-demo.json')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='faultline-simulated-') as temporary:
        directory = Path(temporary)
        flag = directory/'demo.flagd.json'
        shutil.copy(FLAG_FILE, flag)
        before = flag.read_bytes()
        env = FakeEnvironment(flag, watchdog=True)
        env.save(directory/'fake-environment.json')
        store = RunStore(directory/'controller.sqlite3')
        run = execute(make_plan(duration=1), live=True, env=env, store=store,
                      limits=fast_limits(), gate_check=lambda plan, cap: [], log=lambda _:None)
        run_id = run['run_id']
        dx = diagnose_run(store, run_id, directory, 'baseline')
        events = store.events(run_id)
        report = build_report(store.get(run_id), events)
        checks = {'terminal_recovered':run['state']=='RECOVERED',
                  'copied_flags_restored':flag.read_bytes()==before,
                  'real_flags_unchanged':FLAG_FILE.read_bytes()==before,
                  'watchdog_acknowledged':any(e.data.get('watchdog_ready') is True for e in events),
                  'events_contiguous':[e.seq for e in events]==list(range(1,len(events)+1)),
                  'cleanup_verified':report.cleanup_status=='verified'}
        result = {'kind':'simulated_controller_demo','live_acceptance':False,'checks':checks,
                  'report':report.model_dump(mode='json'),'diagnosis':dx.model_dump(mode='json'),
                  'limitations':['All application and telemetry responses use the test environment.',
                                 'No live fault, source-image deployment or matched replay was performed.',
                                 'The simulated phase measurements do not estimate shop performance.']}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps({'kind':result['kind'],'live_acceptance':False,'checks':checks,'output':str(args.out)},indent=2))
        return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
