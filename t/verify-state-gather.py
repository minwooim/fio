#!/usr/bin/env python3
"""
# verify-state-gather.py
#
# Test fio's verify_multiple_jobs feature.
# Each write run saves its own state file (v6, with workload params).
# The verify phase loads all state files in order, dry-runs each to
# populate io_hist_tree, then does a single verify pass.
#
# USAGE
# see python3 t/verify-state-gather.py --help
#
# EXAMPLES
# python3 t/verify-state-gather.py
# python3 t/verify-state-gather.py --fio ./fio
#
# REQUIREMENTS
# Python 3.6
# Linux
#
"""
import copy
import os
import platform
import sys
import json
import time
import logging
import argparse
import subprocess
import tempfile
from pathlib import Path
from fiotestlib import FioJobCmdTest, run_fio_tests
from fiotestcommon import SUCCESS_DEFAULT, SUCCESS_NONZERO, Requirements


class GatherVerifyTest(FioJobCmdTest):
    """
    Multi-phase verify_multiple_jobs test.

    self.fio_opts['_phases']: list of fio arg lists to run before the final
    verify_only phase.  The last list is the verify command, passed to
    super().setup() for normal run/check handling.
    """

    def setup(self, parameters):
        phases = self.fio_opts.get('_phases', [])
        if not phases:
            raise ValueError("_phases must be set in fio_opts")

        self.prior_phases = phases[:-1]
        final_args = phases[-1]
        super().setup(final_args)

    def run(self):
        for i, args in enumerate(self.prior_phases):
            cmd = [self.paths['exe']] + args
            logging.debug("prior phase %d: %s", i, ' '.join(cmd))
            print(" ".join(cmd))
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                self.passed = False
                self.failure_reason += (
                    f" prior phase {i} failed (rc={result.returncode}): "
                    f"{result.stderr.decode(errors='replace')[:200]}"
                )
                return

        super().run()

    def check_result(self):
        super().check_result()

        if not self.passed:
            return

        verify_json_path = self.fio_opts.get('_verify_json_path')
        if not verify_json_path or not self.fio_opts.get('_check_verify_errors'):
            return

        try:
            with open(verify_json_path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.passed = False
            self.failure_reason += f" could not load verify JSON ({verify_json_path}): {e},"
            return

        for job in data['jobs']:
            err = job.get('error', 0)
            if err != 0:
                self.passed = False
                self.failure_reason += (
                    f" unexpected verify errors in job '{job.get('jobname', '?')}': {err},"
                )

        total_read = sum(j.get('read', {}).get('io_bytes', 0) for j in data['jobs'])
        if total_read == 0:
            self.passed = False
            self.failure_reason += " verify phase read 0 bytes (nothing verified),"


def make_write_phase(aux, dut, name, rw, bs, size, extra=None,
                     numjobs=1, offset=None, ioengine='sync'):
    """Return fio arg list for a write run that saves a state file per thread."""
    args = [
        f'--name={name}',
        f'--filename={dut}',
        '--verify=crc32c',
        f'--ioengine={ioengine}',
        f'--aux-path={aux}',
        '--randrepeat=1',
        f'--rw={rw}',
        f'--bs={bs}',
        f'--size={size}',
        '--do_verify=0',
        '--verify_state_save=1',
    ]
    if numjobs > 1:
        args.append(f'--numjobs={numjobs}')
    if offset is not None:
        args.append(f'--offset={offset}')
    if extra:
        args += extra
    return args


def make_verify_phase(aux, dut, job_names, rw, bs, size,
                      numjobs=1, offset=None, extra=None, ioengine='sync'):
    """Return fio arg list for the verify_only phase (writes JSON output)."""
    jobs_str = ','.join(job_names)
    verify_json = os.path.join(aux, 'verify.json')
    args = [
        f'--name={job_names[-1]}',
        f'--filename={dut}',
        '--verify=crc32c',
        f'--ioengine={ioengine}',
        f'--aux-path={aux}',
        '--randrepeat=1',
        f'--rw={rw}',
        f'--bs={bs}',
        f'--size={size}',
        '--verify_only',
        f'--verify_multiple_jobs={jobs_str}',
        '--output-format=json',
        f'--output={verify_json}',
    ]
    if numjobs > 1:
        args.append(f'--numjobs={numjobs}')
    if offset is not None:
        args.append(f'--offset={offset}')
    if extra:
        args += extra
    return args


def make_verify_phase_nojson(aux, dut, job_names, rw, bs, size,
                              numjobs=1, offset=None, extra=None,
                              ioengine='sync'):
    """Return fio arg list for a verify_only phase without JSON output.

    Used as a prior (non-final) phase: failures are caught via exit code.
    """
    jobs_str = ','.join(job_names)
    args = [
        f'--name={job_names[-1]}',
        f'--filename={dut}',
        '--verify=crc32c',
        f'--ioengine={ioengine}',
        f'--aux-path={aux}',
        '--randrepeat=1',
        f'--rw={rw}',
        f'--bs={bs}',
        f'--size={size}',
        '--verify_only',
        f'--verify_multiple_jobs={jobs_str}',
    ]
    if numjobs > 1:
        args.append(f'--numjobs={numjobs}')
    if offset is not None:
        args.append(f'--offset={offset}')
    if extra:
        args += extra
    return args


def build_all_phases(test_list, artifact_root, dut, ioengine='sync'):
    for t in test_list:
        tid = t['fio_opts'].get('_test_id')
        if tid is None:
            continue

        aux = os.path.join(artifact_root, f'vsg{tid:03d}')
        os.makedirs(aux, exist_ok=True)

        if tid == 1:
            # Single sequential write job + verify
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '4M',
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1'], 'write', '4k', '4M',
                                  ioengine=ioengine),
            ]

        elif tid == 2:
            # Two sequential write jobs with same params + verify
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '4M',
                                 ioengine=ioengine),
                make_write_phase(aux, dut, 'job2', 'write', '4k', '4M',
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1', 'job2'], 'write', '4k',
                                  '4M', ioengine=ioengine),
            ]

        elif tid == 100:
            # Heterogeneous: 4k seq then 64k random
            phases = [
                make_write_phase(aux, dut, 'job1', 'write',     '4k',  '4M',
                                 ioengine=ioengine),
                make_write_phase(aux, dut, 'job2', 'randwrite', '64k', '4M',
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1', 'job2'], 'randwrite',
                                  '64k', '4M', ioengine=ioengine),
            ]

        elif tid == 101:
            # Three heterogeneous jobs
            phases = [
                make_write_phase(aux, dut, 'job1', 'write',     '4k',  '4M',
                                 ioengine=ioengine),
                make_write_phase(aux, dut, 'job2', 'randwrite', '64k', '4M',
                                 ioengine=ioengine),
                make_write_phase(aux, dut, 'job3', 'write',     '8k',  '2M',
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1', 'job2', 'job3'], 'write',
                                  '8k', '2M', ioengine=ioengine),
            ]

        elif tid == 102:
            # zipf random distribution: dry-run must restore distribution to
            # reproduce the same random offsets as the original write
            phases = [
                make_write_phase(aux, dut, 'job1', 'randwrite', '4k', '4M',
                                 extra=['--random_distribution=zipf:1.2'],
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1'], 'randwrite', '4k', '4M',
                                  ioengine=ioengine),
            ]

        elif tid == 103:
            # non-zero offset_increment: verify the field is saved and restored
            # without corrupting the dry-run (numjobs=1 so effective offset
            # is unchanged, but the serialization round-trip is exercised)
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '4M',
                                 extra=['--offset_increment=4k'],
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1'], 'write', '4k', '4M',
                                  ioengine=ioengine),
            ]

        elif tid == 110:
            # TC (1): numjobs=2 with offset_increment — each thread owns a
            # dedicated 2M slice ([0,2M) and [2M,4M)).  The verify job mirrors
            # the same numjobs/offset_increment so that verify thread i loads
            # the prior job's state file for thread i.
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '2M',
                                 numjobs=2, extra=['--offset_increment=2M'],
                                 ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1'], 'write', '4k', '2M',
                                  numjobs=2, extra=['--offset_increment=2M'],
                                  ioengine=ioengine),
            ]

        elif tid == 120:
            # TC (2): three numjobs=1 jobs each with an explicit, non-overlapping
            # offset ([0,2M), [2M,4M), [4M,6M)).  A single verify thread
            # dry-runs all three sequentially then reads the full 6M.
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '2M',
                                 offset='0', ioengine=ioengine),
                make_write_phase(aux, dut, 'job2', 'write', '4k', '2M',
                                 offset='2M', ioengine=ioengine),
                make_write_phase(aux, dut, 'job3', 'write', '4k', '2M',
                                 offset='4M', ioengine=ioengine),
                make_verify_phase(aux, dut, ['job1', 'job2', 'job3'],
                                  'write', '4k', '6M',
                                  offset='0', ioengine=ioengine),
            ]

        elif tid == 130:
            # TC (3): numjobs=2 job + separate numjobs=1 job.
            # job1 covers [0,4M) with two threads (offset_increment=2M);
            # job2 covers [4M,6M) with a single thread.
            # Verify is split into two phases (same structure as write):
            #   phase 1 (prior, exit-code checked): verify job1's two slices
            #   phase 2 (final, JSON checked):       verify job2's slice
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '2M',
                                 numjobs=2, extra=['--offset_increment=2M'],
                                 ioengine=ioengine),
                make_write_phase(aux, dut, 'job2', 'write', '4k', '2M',
                                 offset='4M', ioengine=ioengine),
                make_verify_phase_nojson(aux, dut, ['job1'], 'write', '4k',
                                         '2M', numjobs=2,
                                         extra=['--offset_increment=2M'],
                                         ioengine=ioengine),
                make_verify_phase(aux, dut, ['job2'], 'write', '4k', '2M',
                                  offset='4M', ioengine=ioengine),
            ]

        elif tid == 200:
            # Error: verify references a nonexistent job name → should fail
            phases = [
                make_write_phase(aux, dut, 'job1', 'write', '4k', '4M',
                                 ioengine=ioengine),
                [
                    '--name=job1',
                    f'--filename={dut}',
                    '--verify=crc32c',
                    f'--ioengine={ioengine}',
                    f'--aux-path={aux}',
                    '--rw=write',
                    '--bs=4k',
                    '--size=4M',
                    '--verify_only',
                    '--verify_multiple_jobs=nosuchfile',
                ],
            ]

        else:
            continue

        t['fio_opts']['_phases'] = phases
        if tid != 200:
            t['fio_opts']['_verify_json_path'] = os.path.join(aux, 'verify.json')


TEST_LIST = [
    {
        'test_id': 1,
        'description': 'single seq write job + verify_multiple_jobs → verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 1,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 2,
        'description': 'two same-params seq write jobs + verify_multiple_jobs → verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 2,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 100,
        'description': 'heterogeneous: 4k seq + 64k rand, verify_multiple_jobs → verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 100,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 101,
        'description': '3 heterogeneous jobs (4k seq, 64k rand, 8k seq), verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 101,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 102,
        'description': 'zipf random_distribution: dry-run restores distribution → verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 102,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 103,
        'description': 'non-zero offset_increment saved and restored → verify pass',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 103,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 110,
        'description': 'numjobs=2 + offset_increment: each thread verifies its own dedicated slice',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 110,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 120,
        'description': '3 numjobs=1 jobs with explicit offsets, single verify covers full range',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 120,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 130,
        'description': 'numjobs=2 job + separate numjobs=1 job, verify each slice independently',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_DEFAULT,
        'fio_opts': {
            '_test_id': 130,
            '_check_verify_errors': True,
        },
    },
    {
        'test_id': 200,
        'description': 'missing state file in verify_multiple_jobs → nonzero exit',
        'test_class': GatherVerifyTest,
        'success': SUCCESS_NONZERO,
        'fio_opts': {
            '_test_id': 200,
        },
    },
]


def parse_args():
    parser = argparse.ArgumentParser(
        description='Test fio verify_multiple_jobs feature')
    parser.add_argument('-r', '--fio-root', help='fio root path')
    parser.add_argument('-d', '--debug', action='store_true')
    parser.add_argument('-f', '--fio', help='path to fio executable')
    parser.add_argument('-a', '--artifact-root', help='artifact root directory')
    parser.add_argument('-s', '--skip', nargs='+', type=int)
    parser.add_argument('-o', '--run-only', nargs='+', type=int)
    parser.add_argument('-k', '--skip-req', action='store_true')
    parser.add_argument('--dut', help='Block device to test against')
    parser.add_argument('--ioengines',
                        default=None,
                        help='comma-separated list of ioengines to test '
                             '(default: platform-specific async,sync pair, '
                             'e.g. --ioengines psync,libaio)')
    return parser.parse_args()


def main():
    args = parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    artifact_root = args.artifact_root if args.artifact_root else \
        f'verify-state-gather-test-{time.strftime("%Y%m%d-%H%M%S")}'
    os.mkdir(artifact_root)
    artifact_root = os.path.abspath(artifact_root)
    print(f'Artifact directory is {artifact_root}')

    if args.fio:
        fio_path = str(Path(args.fio).absolute())
    else:
        fio_path = os.path.join(os.path.dirname(__file__), '../fio')
    print(f'fio path is {fio_path}')

    if args.fio_root:
        fio_root = args.fio_root
    else:
        fio_root = str(Path(__file__).absolute().parent.parent)
    print(f'fio root is {fio_root}')

    if not args.skip_req:
        Requirements(fio_root, args)

    cleanup_dut = None
    if not args.dut:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.vsg-dut')
        tmp.close()
        os.truncate(tmp.name, 8 * 1024 * 1024)
        args.dut = tmp.name
        cleanup_dut = tmp.name
        print(f'Using temporary file as DUT: {tmp.name}')

    if args.ioengines:
        engines = [e.strip() for e in args.ioengines.split(',')]
    elif platform.system() == 'Linux':
        engines = ['io_uring', 'psync']
    elif platform.system() == 'Windows':
        engines = ['windowsaio', 'sync']
    else:
        engines = ['posixaio', 'psync']

    total_passed = total_failed = total_skipped = 0

    for engine in engines:
        engine_tests = copy.deepcopy(TEST_LIST)
        engine_artifact = os.path.join(artifact_root, engine)
        os.mkdir(engine_artifact)

        build_all_phases(engine_tests, engine_artifact, args.dut,
                         ioengine=engine)

        test_env = {
            'fio_path': fio_path,
            'fio_root': fio_root,
            'artifact_root': engine_artifact,
            'basename': 'vsg',
        }

        print(f'\nRunning with ioengine={engine}')
        p, f, s = run_fio_tests(engine_tests, test_env, args)
        total_passed += p
        total_failed += f
        total_skipped += s

    if cleanup_dut:
        os.unlink(cleanup_dut)

    print(f"\nResults: {total_passed} passed, {total_failed} failed, "
          f"{total_skipped} skipped")
    sys.exit(0 if total_failed == 0 else 1)


if __name__ == '__main__':
    main()
