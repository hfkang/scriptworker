#!/usr/bin/env python
# coding=utf-8
"""Scriptworker integration tests.
"""
import aiohttp
import arrow
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import pytest
import slugid
from scriptworker.config import CREDS_FILES, DEFAULT_CONFIG, read_worker_creds
from scriptworker.context import Context
import scriptworker.log as swlog
import scriptworker.worker as worker
import scriptworker.utils as utils
from taskcluster.async import Queue

TIMEOUT_SCRIPT = os.path.join(os.path.dirname(__file__), "data", "long_running.py")
SKIP_REASON = "NO_TESTS_OVER_WIRE: skipping integration test"


def read_integration_creds():
    creds = read_worker_creds(key="integration_credentials")
    if creds:
        return creds
    raise Exception(
        """To run integration tests, put your worker-test clientId creds, in json format,
in one of these files:

    {files}

with the format

    {{"integration_credentials": {{"accessToken": "...", "clientId": "...", "certificate": "..."}}}}

(only specify "certificate" if using temporary credentials)

This clientId will need the scope assume:project:taskcluster:worker-test-scopes

To skip integration tests, set the environment variable NO_TESTS_OVER_WIRE""".format(files=CREDS_FILES)
    )


def build_config(override):
    cwd = os.getcwd()
    basedir = os.path.join(cwd, "integration")
    if not os.path.exists(basedir):
        os.makedirs(basedir)
    randstring = slugid.nice()[0:6].decode('utf-8')
    config = deepcopy(DEFAULT_CONFIG)
    config.update({
        'log_dir': os.path.join(basedir, "log"),
        'artifact_dir': os.path.join(basedir, "artifact"),
        'work_dir': os.path.join(basedir, "work"),
        "worker_type": "dummy-worker-{}".format(randstring),
        "worker_id": "dummy-worker-{}".format(randstring),
        'artifact_upload_timeout': 60 * 2,
        'artifact_expiration_hours': 1,
        'reclaim_interval': 5,
        'credential_update_interval': .1,
        'task_script': ('bash', '-c', '>&2 echo bar && echo foo && sleep 9 && exit 2'),
        'task_max_timeout': 60,
    })
    creds = read_integration_creds()
    if isinstance(override, dict):
        config.update(override)
    with open(os.path.join(basedir, "secrets.json"), "w") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
    return config, creds


@contextmanager
def get_context(config_override):
    context = Context()
    context.config, credentials = build_config(config_override)
    swlog.update_logging_config(context)
    utils.cleanup(context)
    with aiohttp.ClientSession() as session:
        context.session = session
        context.credentials = credentials
        context.queue = Queue({"credentials": credentials}, session=session)
        yield context


async def create_task(context, task_id, task_group_id):
    task_group_id = task_group_id or slugid.nice().decode('utf-8')
    now = arrow.utcnow()
    deadline = now.replace(hours=1)
    expires = now.replace(days=3)
    payload = {
        'provisionerId': context.config['provisioner_id'],
        'schedulerId': context.config['scheduler_id'],
        'workerType': context.config['worker_type'],
        'taskGroupId': task_group_id,
        'dependencies': [],
        'requires': 'all-completed',
        'routes': [],
        'priority': 'normal',
        'retries': 5,
        'created': now.isoformat(),
        'deadline': deadline.isoformat(),
        'expires': expires.isoformat(),
        'scopes': [],
        'payload': {
        },
        'metadata': {
            'name': 'ScriptWorker Integration Test',
            'description': 'ScriptWorker Integration Test',
            'owner': 'release+python@mozilla.com',
            'source': 'https://github.com/escapewindow/scriptworker/'
        },
        'tags': {},
        'extra': {
            'test_extra': 1,
        }
    }
    return await context.queue.createTask(task_id, payload)


async def task_status(context, task_id):
    return await context.queue.status(task_id)


@contextmanager
def remember_cwd():
    """http://stackoverflow.com/a/170174
    """
    curdir = os.getcwd()
    try:
        yield
    finally:
        os.chdir(curdir)


class TestIntegration(object):
    @pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
    @pytest.mark.asyncio
    async def test_run_successful_task(self, event_loop):
        task_id = slugid.nice().decode('utf-8')
        task_group_id = slugid.nice().decode('utf-8')
        with get_context(None) as context:
            result = await create_task(context, task_id, task_group_id)
            assert result['status']['state'] == 'pending'
            with remember_cwd():
                os.chdir("integration")
                status = await worker.run_loop(context, creds_key="integration_credentials")
            assert status == 2
            result = await task_status(context, task_id)
            assert result['status']['state'] == 'failed'

    @pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
    def test_run_maxtimeout(self, event_loop):
        task_id = slugid.nice().decode('utf-8')
        task_group_id = slugid.nice().decode('utf-8')
        partial_config = {
            'task_max_timeout': 2,
        }
        with get_context(partial_config) as context:
            result = event_loop.run_until_complete(
                create_task(context, task_id, task_group_id)
            )
            assert result['status']['state'] == 'pending'
            with remember_cwd():
                os.chdir("integration")
                with pytest.raises(RuntimeError):
                    event_loop.run_until_complete(
                        worker.run_loop(context, creds_key="integration_credentials")
                    )
                    # Because we're using asyncio to kill tasks in the loop,
                    # we're going to hit a RuntimeError
            result = event_loop.run_until_complete(task_status(context, task_id))
            # TODO We need to be able to ensure this is 'failed'.
            assert result['status']['state'] in ('failed', 'running')

    @pytest.mark.skipif(os.environ.get("NO_TESTS_OVER_WIRE"), reason=SKIP_REASON)
    @pytest.mark.asyncio
    async def test_empty_queue(self, event_loop):
        with get_context(None) as context:
            with remember_cwd():
                os.chdir("integration")
                status = await worker.run_loop(context, creds_key="integration_credentials")
            assert status is None
