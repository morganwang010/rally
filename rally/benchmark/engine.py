# Copyright 2013: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import json
import jsonschema
import os
import tempfile

from rally.benchmark import base
from rally.benchmark import config
from rally.benchmark import utils
from rally import consts
from rally import exceptions
from rally.openstack.common.gettextutils import _  # noqa
from rally.openstack.common import log as logging

LOG = logging.getLogger(__name__)


class TestEngine(object):
    """The test engine class, an instance of which is initialized by the
    Orchestrator with the test configuration and then is used to launch OSTF
    tests and to benchmark the deployment.

    .. note::

        Typical usage:
            ...
            test = TestEngine(test_config)
            # Deploying the cloud...
            with test.bind(cloud_config):
                test.verify()
                test.benchmark()
    """

    def __init__(self, test_config, task):
        """TestEngine constructor.

        :param test_config: Dictionary of form {
            "verify": {
                "tests_to_run": ["sanity", "snapshot", "smoke"]
            },
            "benchmark": {
                "tests_setUp": {
                    "nova.server_metadata": {"servers_to_boot": 10}
                }
                "tests_to_run": {
                    "nova.server_metadata.test_set_and_delete_meta": [
                        {"args": {"amount": 5}, "times": 1, "concurrent": 1},
                        {"args": {"amount": 10}, "times": 4, "concurrent": 2}
                    ]
                }
            }
        }
        :param task: The current task which is being performed
        """
        self.task = task

        # NOTE(msdubov): self.verification_tests is a dict since it has
        #                to contain pytest running args, while
        #                self.benchmark_scenarios is just a list of names.
        self.verification_tests = utils.Verifier.list_verification_tests()
        self.benchmark_scenarios = base.Scenario.list_benchmark_scenarios()

        self._validate_test_config(test_config)
        test_config = self._format_test_config(test_config)
        self.test_config = config.TestConfigManager(test_config)

    def _validate_test_config(self, test_config):
        """Checks whether the given test config is valid and can be used during
        verification and benchmarking tests.

        :param test_config: Dictionary in the same format as for the __init__
                            method.

        :raises: Exception if the test config is not valid
        """
        task_uuid = self.task['uuid']
        LOG.info(_('Task %s: Validating the passed test config...') %
                 task_uuid)
        # Perform schema validation
        try:
            jsonschema.validate(test_config, config.test_config_schema)
        except jsonschema.ValidationError as e:
            LOG.exception(_('Task %s: Error: %s') % (task_uuid, e.message))
            raise exceptions.InvalidConfigException(message=e.message)

        # Check for verification test names
        for test in test_config['verify']['tests_to_run']:
            if test not in self.verification_tests:
                LOG.exception(_('Task %s: Error: the specified '
                                'verification test does not exist: %s') %
                              (task_uuid, test))
                raise exceptions.NoSuchVerificationTest(test_name=test)
        # Check for benchmark scenario names
        benchmark_scenarios_set = set(self.benchmark_scenarios)
        for scenario in test_config['benchmark']['tests_to_run']:
            if scenario not in benchmark_scenarios_set:
                LOG.exception(_('Task %s: Error: the specified '
                                'benchmark scenario does not exist: %s') %
                              (task_uuid, scenario))
                raise exceptions.NoSuchScenario(name=scenario)
        LOG.info(_('Task %s: Test config validation succeeded.') % task_uuid)

    def _format_test_config(self, test_config):
        """Returns a formatted copy of the given valid test config so that
        it can be used during verification and benchmarking tests.

        :param test_config: Dictionary in the same format as for the __init__
                            method.

        :returns: Dictionary
        """
        task_uuid = self.task['uuid']
        LOG.debug(_('Task %s: Formatting the given test config...') %
                  task_uuid)
        formatted_test_config = copy.deepcopy(test_config)
        # NOTE(msdubov): if 'verify' or 'benchmark' tests are not specified,
        #                run them all by default.
        if ('verify' not in formatted_test_config or
           'tests_to_run' not in formatted_test_config['verify']):
            formatted_test_config['verify'] = {
                'tests_to_run': self.verification_tests.keys()
            }
        LOG.debug(_('Task %s: Test config formatting succeeded.') % task_uuid)
        return formatted_test_config

    def __enter__(self):
        task_uuid = self.task['uuid']
        LOG.debug(_('Task %s: Writing cloud & test configs into '
                    'temporary files...') % task_uuid)
        with os.fdopen(self.cloud_config_fd, 'w') as f:
            self.cloud_config.write(f)
        with os.fdopen(self.test_config_fd, 'w') as f:
            self.test_config.write(f)
        LOG.debug(_('Task %s: Completed writing temporary '
                    'config files.') % task_uuid)

    def __exit__(self, type, value, traceback):
        task_uuid = self.task['uuid']
        LOG.debug(_('Task %s: Deleting temporary config files...') % task_uuid)
        os.remove(self.cloud_config_path)
        os.remove(self.test_config_path)
        LOG.debug(_('Task %s: Completed deleting temporary '
                    'config files.') % task_uuid)

    def bind(self, cloud_config):
        """Binds an existing deployment configuration to the test engine.

        :param cloud_config: The deployment configuration, which sould be
                             passed as a two-level dictionary: the top-level
                             keys should be section names while the keys on
                             the second level should represent option names.
                             E.g., see the default cloud configuration in the
                             rally.benchmark.config.CloudConfigManager class.

        :returns: self (the method should be called in a 'with' statement)
        """
        task_uuid = self.task['uuid']
        LOG.info(_('Task %s: Binding the cloud config...') % task_uuid)
        self.cloud_config = config.CloudConfigManager()
        self.cloud_config.read_from_dict(cloud_config)
        LOG.info(_('Task %s: Successfuly bound the cloud config.') % task_uuid)

        self.cloud_config_fd, self.cloud_config_path = tempfile.mkstemp(
                                                suffix='rallycfg', text=True)
        self.test_config_fd, self.test_config_path = tempfile.mkstemp(
                                                suffix='rallycfg', text=True)

        return self

    def verify(self):
        """Runs OSTF tests to verify the current cloud deployment.

        :raises: VerificationException if some of the verification tests failed
        """
        task_uuid = self.task['uuid']
        self.task.update_status(consts.TaskStatus.TEST_TOOL_VERIFY_OPENSTACK)
        LOG.info(_('Task %s: Verifying the cloud deployment...') % task_uuid)
        verifier = utils.Verifier(self.task, self.cloud_config_path)
        tests_to_run = self.test_config.to_dict()['verify']['tests_to_run']
        verification_tests = dict((test, self.verification_tests[test])
                                  for test in tests_to_run)
        test_run_results = verifier.run_all(verification_tests)
        self.task.update_verification_log(json.dumps(test_run_results))
        for result in test_run_results:
            if result['status'] != 0:
                error_msg = result['msg']
                LOG.exception(_('Task %s: One of verification '
                                'tests failed: %s') % (task_uuid, error_msg))
                raise exceptions.DeploymentVerificationException(
                                                    test_message=error_msg)
        LOG.info(_('Task %s: Verification succeeded.') % task_uuid)

    def benchmark(self):
        """Runs the benchmarks according to the test configuration
        the test engine was initialized with.

        :returns: List of dicts, each dict containing the results of all the
                  corresponding benchmark test launches
        """

        task_uuid = self.task['uuid']
        self.task.update_status(consts.TaskStatus.TEST_TOOL_BENCHMARKING)
        LOG.info(_('Task %s: Launching benchmark scenarios...') % task_uuid)

        runer = utils.ScenarioRunner(self.task,
                                     self.cloud_config.to_dict()["identity"])

        results = {}
        scenarios = self.test_config.to_dict()['benchmark']['tests_to_run']
        for name in scenarios:
            for n, kwargs in enumerate(scenarios[name]):
                key = {'name': name, 'pos': n, 'kw': kwargs}
                result = runer.run(name, kwargs)
                self.task.append_results(key, {"raw": result})
                results[json.dumps(key)] = result

        LOG.info(_('Task %s: Completed benchmarking.') % task_uuid)
        return results
