#!/usr/bin/env python3
"""One bounded scenario, controlled by the parent runner (not a public CLI)."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import signal
import sys
import time
import traceback

from adapters import upstream_case
from common import digest, read_json, write_json
from execution_contracts import check_request
from lifecycle import Instance
from profiles import verify_binding
from schema_check import check
import scenarios
from wire import Blocked, Trace


class Cancelled(BaseException):
    pass


def main(request_path):
    request = read_json(request_path)
    check_request(request, request_path, before_execution=True)
    trace = Trace(Path(request['trace']))
    start = time.monotonic()
    status, error, output = 'PASS', None, io.StringIO()

    def cancel(signum, _frame):
        raise Cancelled('worker interrupted: ' + str(signum))

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancel)
    trace.event('case-start', run_id=request['run_id'], attempt_id=request['attempt_id'],
                target=request['target'], case_id=request['case']['id'], profile_binding=request.get('profile_binding'))
    try:
        binding = verify_binding(request)
        trace.event('profile-checked', binding=binding)
        with redirect_stdout(output), redirect_stderr(output):
            case = request['case']
            def execute(endpoint):
                if case['runner'] == 'upstream':
                    item = case['upstream']
                    upstream_case(Path(request['upstream']), item['file'], endpoint, trace, item['parameters'])
                else:
                    getattr(scenarios, case['entrypoint'])(endpoint, trace)
            if case['runner'] == 'calibration':
                scenarios.calibration(trace)
            elif request['resources'] is not None:
                with Instance(request['image'], request['target'], Path(request['config']), request['resources'], trace) as instance:
                    execute(instance.endpoint)
            else:
                trace.event('live-target', endpoint=request['endpoint'], profile=case['profile'])
                execute(tuple(request['endpoint']))
    except Blocked as failure:
        status, error = 'BLOCKED', str(failure)
        output.write(traceback.format_exc())
    except (AssertionError, SystemExit) as failure:
        status, error = 'FAIL', repr(failure)
        output.write(traceback.format_exc())
    except BaseException as failure:
        status, error = 'ERROR', repr(failure)
        output.write(traceback.format_exc())
    trace.event('case-end', status=status)
    Path(request['log']).write_text(output.getvalue())
    code = {'PASS': 0, 'FAIL': 1, 'BLOCKED': 3, 'ERROR': 3}[status]
    result = dict(schema_version=1, run_id=request['run_id'], attempt_id=request['attempt_id'],
               target=request['target'], case_id=request['case']['id'], status=status, error=error,
               profile_binding=request.get('profile_binding'),
               seconds=time.monotonic() - start, evidence=trace.path.name,
               evidence_sha256=digest(trace.path), evidence_bytes=trace.path.stat().st_size)
    check('worker-result', result)
    write_json(request['result'], result)
    return code


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
