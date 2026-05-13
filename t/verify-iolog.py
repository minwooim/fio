#!/usr/bin/env python3
"""
verify-iolog.py

Test fio's verify_only=1 + read_iolog multi-session workloads.

Both write sessions use the same --write_iolog path so fio appends the second
session's entries to the first session's file.  The combined log is fed
directly to verify_only.

USAGE
  python3 t/verify-iolog.py --file /tmp/fio-iolog-test.dat

REQUIREMENTS
  Python 3.6
  Linux
"""
import copy
import json
import locale
import logging
import os
import platform
import stat
import subprocess
import sys
import time
import argparse
from pathlib import Path

from fiotestlib import FioJobCmdTest, run_fio_tests
from fiotestcommon import Requirements


class VerifyIologTest(FioJobCmdTest):
    """
    Three-phase test: write1 → write2 → verify_only.

    Both write phases append to the same iolog file; verify_only reads it.
    """

    def __init__(self, fio_path, success, testnum, artifact_root, fio_opts,
                 basename=None):
        super().__init__(fio_path, success, testnum, artifact_root, fio_opts,
                         basename)
        stub = os.path.join(self.paths['test_dir'],
                            f"{self.basename}{self.testnum:03d}")
        self.filenames['iolog']     = os.path.abspath(f"{stub}.iolog")
        self.filenames['output_w1'] = os.path.abspath(f"{stub}.write1.output")
        self.filenames['output_w2'] = os.path.abspath(f"{stub}.write2.output")

    def setup(self, parameters):
        if not os.path.exists(self.paths['test_dir']):
            os.mkdir(self.paths['test_dir'])
        self.parameters = []

    def _base_args(self, name, phase_output):
        args = [
            f"--name={name}",
            f"--ioengine={self.fio_opts['ioengine']}",
            f"--filename={self.fio_opts['filename']}",
            f"--verify={self.fio_opts['verify']}",
            "--output-format=json",
            f"--output={phase_output}",
        ]
        if self.fio_opts.get('direct'):
            args.append(f"--direct={self.fio_opts['direct']}")
        return args

    def _write_args(self, phase, phase_output):
        opts = self.fio_opts[phase]
        args = self._base_args(phase, phase_output) + [
            f"--rw={opts['rw']}",
            f"--bs={opts['bs']}",
            f"--size={opts['size']}",
            "--do_verify=0",
            f"--write_iolog={self.filenames['iolog']}",
        ]
        if 'offset' in opts:
            args.append(f"--offset={opts['offset']}")
        return args

    def _verify_args(self):
        return self._base_args('verify', self.filenames['output']) + [
            "--do_verify=1",
            "--verify_only=1",
            f"--read_iolog={self.filenames['iolog']}",
            "--thread=1",
        ]

    def _run_one(self, args, stdout_f, stderr_f, ec_f):
        command = [self.paths['exe']] + args
        with open(self.filenames['cmd'], 'a',
                  encoding=locale.getpreferredencoding()) as cf:
            cf.write(' \\\n '.join(command) + '\n\n')
        proc = subprocess.Popen(command,
                                stdout=stdout_f,
                                stderr=stderr_f,
                                cwd=self.paths['test_dir'],
                                universal_newlines=True)
        proc.communicate(timeout=self.success['timeout'])
        ec_f.write(f'{proc.returncode}\n')
        logging.debug('Test %d %s: exit %d', self.testnum, args[0], proc.returncode)
        return proc

    @staticmethod
    def _is_block_device(path):
        try:
            return stat.S_ISBLK(os.stat(path).st_mode)
        except OSError:
            return False

    def run(self):
        fname = self.fio_opts['filename']
        if not self._is_block_device(fname):
            # Pre-allocate regular file so both write sessions fit.
            file_size = self.fio_opts.get('file_size', 64 * 1024)
            with open(fname, 'ab'):
                pass
            os.truncate(fname, file_size)

        # Remove any stale iolog from a previous run.
        try:
            os.remove(self.filenames['iolog'])
        except FileNotFoundError:
            pass

        try:
            with open(self.filenames['stdout'], 'w',
                      encoding=locale.getpreferredencoding()) as so, \
                 open(self.filenames['stderr'], 'w',
                      encoding=locale.getpreferredencoding()) as se, \
                 open(self.filenames['exitcode'], 'w',
                      encoding=locale.getpreferredencoding()) as ec:

                p1 = self._run_one(
                    self._write_args('write1', self.filenames['output_w1']),
                    so, se, ec)
                if p1.returncode != 0:
                    self.output['proc'] = p1
                    self.output['failure'] = f'write1 exited {p1.returncode}'
                    return

                p2 = self._run_one(
                    self._write_args('write2', self.filenames['output_w2']),
                    so, se, ec)
                if p2.returncode != 0:
                    self.output['proc'] = p2
                    self.output['failure'] = f'write2 exited {p2.returncode}'
                    return

                pv = self._run_one(self._verify_args(), so, se, ec)
                self.output['proc'] = pv

        except subprocess.TimeoutExpired:
            self.output['failure'] = 'timeout'
        except Exception:
            self.output['failure'] = 'exception'
            self.output['exc_info'] = sys.exc_info()

    @staticmethod
    def _load_json(path):
        try:
            with open(path, 'r', encoding=locale.getpreferredencoding()) as f:
                raw = f.read()
            lines = raw.splitlines()
            last = len(lines) - lines[::-1].index('}')
            return json.loads('\n'.join(lines[lines.index('{'):last]))
        except Exception as exc:
            logging.debug('JSON parse error in %s: %s', path, exc)
            return None

    def check_result(self):
        if 'proc' not in self.output:
            self.failure_reason = self.output.get('failure', 'did not run')
            self.passed = False
            return

        if 'failure' in self.output:
            self.failure_reason = self.output['failure']
            self.passed = False

        if self.output['proc'].returncode != 0:
            self.failure_reason += (
                f' verify phase exited {self.output["proc"].returncode}')
            self.passed = False

        jv = self._load_json(self.filenames['output'])
        if not jv:
            self.failure_reason += ' cannot parse verify JSON'
            self.passed = False
            return

        job = jv['jobs'][0]
        verify_errors = job.get('read', {}).get('verify_errors', 0)
        if verify_errors != 0:
            self.failure_reason += f' {verify_errors} verify error(s)'
            self.passed = False


TEST_LIST = [
    # TC 1 — same bs (4k), complete overlap
    {
        "test_id": 1,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "write",     "bs": "4k", "size": "32k"},
            "write2": {"rw": "write",     "bs": "4k", "size": "32k"},
            "file_size": 32 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 2 — 4k → 8k, complete overlap
    {
        "test_id": 2,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "randwrite", "bs": "4k", "size": "32k"},
            "write2": {"rw": "write",     "bs": "8k", "size": "32k"},
            "file_size": 32 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 3 — 8k → 4k, complete overlap
    {
        "test_id": 3,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "write",     "bs": "8k", "size": "32k"},
            "write2": {"rw": "randwrite", "bs": "4k", "size": "32k"},
            "file_size": 32 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 4 — same bs (4k), partial overlap: write1 [0,32k) + write2 [16k,48k)
    {
        "test_id": 4,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "write", "bs": "4k", "size": "32k", "offset": "0"},
            "write2": {"rw": "write", "bs": "4k", "size": "32k", "offset": "16k"},
            "file_size": 64 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 5 — 4k → 8k, partial overlap: write1 [0,32k) in 4k + write2 [16k,48k) in 8k
    {
        "test_id": 5,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "write", "bs": "4k", "size": "32k", "offset": "0"},
            "write2": {"rw": "write", "bs": "8k", "size": "32k", "offset": "16k"},
            "file_size": 64 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 6 — 4k -> 4k, no overlap, but read-only workload should not affect verify
    {
        "test_id": 6,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "write", "bs": "4k", "size": "32k", "offset": "0"},
            "write2": {"rw": "read", "bs": "4k", "size": "32k", "offset": "0"},
            "file_size": 64 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
    # TC 7 — Various write, randrw, randread and verify them at once
    {
        "test_id": 7,
        "fio_opts": {
            "filename": None,
            "verify": "crc32c",
            "write1": {"rw": "randwrite", "bs": "4k", "size": "32k", "offset": "0"},
            "write2": {"rw": "randrw", "bs": "4k", "size": "32k", "offset": "16k"},
            "read1": {"rw": "randread", "bs": "8k", "size": "24k", "offset": "8k"},
            "file_size": 64 * 1024,
        },
        "test_class": VerifyIologTest,
        "requirements": [Requirements.linux],
    },
]


def parse_args():
    parser = argparse.ArgumentParser(
        description='Test verify_only=1 + read_iolog multi-session workflow')
    parser.add_argument('-f', '--fio',
                        help='path to fio executable (default: ../fio)')
    parser.add_argument('-a', '--artifact-root',
                        help='artifact root directory')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='enable debug messages')
    parser.add_argument('-s', '--skip', nargs='+', type=int,
                        help='test IDs to skip')
    parser.add_argument('-o', '--run-only', nargs='+', type=int,
                        help='run only these test IDs')
    parser.add_argument('-k', '--skip-req', action='store_true',
                        help='skip requirements checking')
    parser.add_argument('--ioengines',
                        help='comma-separated engine list '
                             '(default: platform-appropriate sync+async pair)')
    parser.add_argument('--file', required=True,
                        help='target file for I/O (will be overwritten)')
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    artifact_root = args.artifact_root or \
        f"verify-iolog-test-{time.strftime('%Y%m%d-%H%M%S')}"
    os.mkdir(artifact_root)
    print(f"Artifact directory is {artifact_root}")

    fio_path = str(Path(args.fio).absolute()) if args.fio else \
        os.path.join(os.path.dirname(__file__), '../fio')
    print(f"fio path is {fio_path}")

    fio_root = str(Path(__file__).absolute().parent.parent)

    if not args.skip_req:
        Requirements(fio_root, args)

    for test in TEST_LIST:
        test['fio_opts']['filename'] = args.file

    if args.ioengines:
        engines = [e.strip() for e in args.ioengines.split(',')]
    elif platform.system() == 'Linux':
        engines = ['io_uring', 'psync']
    elif platform.system() == 'Windows':
        engines = ['windowsaio', 'sync']
    else:
        engines = ['posixaio', 'psync']

    total_failed = 0
    for engine in engines:
        engine_tests = copy.deepcopy(TEST_LIST)
        for test in engine_tests:
            test['fio_opts']['ioengine'] = engine

        engine_dir = os.path.join(artifact_root, engine)
        os.mkdir(engine_dir)

        test_env = {
            'fio_path':      fio_path,
            'fio_root':      fio_root,
            'artifact_root': engine_dir,
            'basename':      'verify-iolog',
        }

        print(f"\nRunning with ioengine={engine}")
        _, failed, _ = run_fio_tests(engine_tests, test_env, args)
        total_failed += failed

    sys.exit(total_failed)


if __name__ == '__main__':
    main()
