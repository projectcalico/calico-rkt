# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from mock import patch, MagicMock, Mock, call, ANY
from nose.tools import assert_equal, assert_true, assert_false, assert_raises
from nose_parameterized import parameterized

from pycalico.datastore import DatastoreClient
from pycalico.datastore_datatypes import Endpoint, Rule, Rules

from calico import CniPlugin
from calico_cni.constants import ERR_CODE_GENERIC
from calico_cni.policy_drivers import (ApplyProfileError,
                                       get_policy_driver,
                                       PolicyException,
                                       DefaultPolicyDriver,
                                       MesosPolicyDriver,
                                       KubernetesNoPolicyDriver,
                                       KubernetesAnnotationDriver,
                                       KubernetesPolicyDriver)


class DefaultPolicyDriverTest(unittest.TestCase):
    """
    Test class for DefaultPolicyDriver class.
    """
    def setUp(self):
        self.network_name = "test_net_name"
        self.driver = DefaultPolicyDriver(self.network_name)
        assert_equal(self.driver.profile_name, self.network_name)

        # Mock the DatastoreClient
        self.client = MagicMock(spec=DatastoreClient)
        self.driver._client = self.client

    def test_apply_new_profile(self):
        # Mock.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.profile_ids = []
        endpoint.endpoint_id = "12345"
        self.client.profile_exists.return_value = False

        # Mock out generate_tags.
        self.driver.generate_tags = MagicMock(spec=self.driver.generate_tags)
        self.driver.generate_tags.return_value = ["tag1", "tag2"]

        # Call
        self.driver.apply_profile(endpoint)

        # Assert
        self.client.append_profiles_to_endpoint.assert_called_once_with(
                profile_names=[self.network_name], endpoint_id="12345"
        )

    def test_apply_same_profile(self):
        # Mock.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.profile_ids = [self.network_name]
        endpoint.endpoint_id = "12345"
        self.client.profile_exists.return_value = False

        # Call
        self.driver.apply_profile(endpoint)

        # Assert
        assert_false(self.client.append_profiles_to_endpoint.called)

    def test_apply_profile_error(self):
        # Mock.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.profile_ids = []
        endpoint.endpoint_id = "12345"
        endpoint.name = "cali12345"
        self.client.profile_exists.return_value = False
        self.client.append_profiles_to_endpoint.side_effect = KeyError

        # Call
        assert_raises(ApplyProfileError, self.driver.apply_profile, endpoint)

    def test_remove_profile(self):
        # Should do nothing.
        self.driver.remove_profile()

    @parameterized.expand([
        ("invalid=name"), ("^regex$"),
    ])
    def test_invalid_network_name(self, net_name):
        assert_raises(ValueError, DefaultPolicyDriver, net_name)


class MesosPolicyDriverTest(unittest.TestCase):
    def setUp(self):
        self.network_name = "test_net_name"
        self.network_args = {
          "org.apache.mesos" : {
            "network_info" : {
              "name" : "test_net_name",
              "labels" : {
                "labels" : [
                  { "key" : "app", "value" : "myapp" },
                  { "key" : "env", "value" : "prod" }
                ]
              }
            }
          }
        }

        # The init function for Mesos Policy driver will convert these
        # to a standard dictionary.
        self.expected_labels = {"app":"myapp", "env":"prod"}

        self.driver = MesosPolicyDriver(self.network_name, self.network_args)
        assert_equal(self.driver.profile_name, self.network_name)
        assert_equal(self.driver.labels, self.expected_labels)

        # Mock the datastore client
        self.client = MagicMock(spec=DatastoreClient)
        self.driver._client = self.client

    @patch("calico_cni.policy_drivers.DefaultPolicyDriver.apply_profile")
    def test_apply_profile(self, m_default_apply_profile):
        endpoint = MagicMock(spec=Endpoint)
        endpoint.endpoint_id = "12345"

        # Mock default profile call
        self.driver.apply_profile(endpoint)

        m_default_apply_profile.assert_called_once()
        self.assertEqual(endpoint.labels, self.expected_labels)


class KubernetesDefaultPolicyDriverTest(unittest.TestCase):
    """
    Test class for KubernetesDefaultPolicyDriver class.
    """
    def setUp(self):
        self.network_name = "test_net_name"
        self.driver = KubernetesNoPolicyDriver(self.network_name)
        assert_equal(self.driver.profile_name, self.network_name)

        # Mock the DatastoreClient
        self.client = MagicMock(spec=DatastoreClient)
        self.driver._client = self.client

    def test_generate_rules(self):
        # Generate rules
        rules = self.driver.generate_rules()

        # Assert correct.
        expected = Rules(id=self.network_name,
                         inbound_rules=[Rule(action="allow")],
                         outbound_rules=[Rule(action="allow")])
        assert_equal(rules, expected)


class KubernetesAnnotationDriverTest(unittest.TestCase):
    """
    Test class for KubernetesAnnotationDriver class.
    """
    def setUp(self):
        self.pod_name = "podname"
        self.namespace = "namespace"
        self.profile_name = "namespace_podname"
        self.auth_token = "authtoken12345"
        self.api_root = "https://10.100.0.1:443/api/v1/"
        self.network_config = {
                "policy": {}
        }
        self.driver = KubernetesAnnotationDriver(self.pod_name, self.namespace,
                self.auth_token, self.api_root, None, None, None, None)
        assert_equal(self.driver.profile_name, self.profile_name)

        # Mock the DatastoreClient
        self.client = MagicMock(spec=DatastoreClient)
        self.driver._client = self.client

    @patch("calico_cni.policy_drivers.KubernetesAnnotationDriver._get_api_pod", autospec=True)
    def test_generate_rules_mainline(self, m_get_pod):
        # Generate rules
        rules = self.driver.generate_rules()

        # Assert correct. Should return rules which isolate the pod by namespace.
        expected = Rules(id=self.profile_name,
                         inbound_rules=[Rule(action="allow", src_tag="namespace_namespace")],
                         outbound_rules=[Rule(action="allow")])
        assert_equal(rules, expected)

    @patch("calico_cni.policy_drivers.KubernetesAnnotationDriver._get_api_pod", autospec=True)
    def test_generate_rules_kube_system(self, m_get_pod):
        # Use kube-system namespace.
        self.driver.namespace = "kube-system"

        # Generate rules
        rules = self.driver.generate_rules()

        # Assert correct. Should allow all.
        expected = Rules(id=self.profile_name,
                         inbound_rules=[Rule(action="allow")],
                         outbound_rules=[Rule(action="allow")])
        assert_equal(rules, expected)

    def test_remove_profile(self):
        self.driver.remove_profile()
        self.driver._client.remove_profile.assert_called_once_with(self.driver.profile_name)

    def test_remove_missing_profile(self):
        self.driver._client.remove_profile.side_effect = KeyError

        # Nothing should be raised.
        self.driver.remove_profile()

    @patch("calico_cni.policy_drivers.KubernetesAnnotationDriver._get_api_pod", autospec=True)
    def test_generate_rules_annotations(self, m_get_pod):
        # Mock get_metadata to return annotations.
        annotations = {"projectcalico.org/policy": "allow tcp"}
        self.driver._get_metadata = MagicMock(spec=self.driver._get_metadata)
        self.driver._get_metadata.return_value = annotations

        # Generate rules
        rules = self.driver.generate_rules()

        # Assert correct. Should allow all.
        expected = Rules(id=self.profile_name,
                         inbound_rules=[Rule(action="allow", protocol="tcp")],
                         outbound_rules=[Rule(action="allow")])
        assert_equal(rules, expected)

    @patch("calico_cni.policy_drivers.KubernetesAnnotationDriver._get_api_pod", autospec=True)
    def test_generate_rules_parse_error(self, m_get_pod):
        # Mock get_metadata to return annotations.
        annotations = {"projectcalico.org/policy": "allow tcp"}
        self.driver._get_metadata = MagicMock(spec=self.driver._get_metadata)
        self.driver._get_metadata.return_value = annotations

        # Mock to raise error
        self.driver.policy_parser = MagicMock(spec=self.driver.policy_parser)
        self.driver.policy_parser.parse_line.side_effect = ValueError

        # Generate rules
        assert_raises(ApplyProfileError, self.driver.generate_rules)

    def test_generate_tags(self):
        # Mock get_metadata to return labels.
        labels = {"key": "value"}
        self.driver._get_metadata = MagicMock(spec=self.driver._get_metadata)
        self.driver._get_metadata.return_value = labels

        # Call
        tags = self.driver.generate_tags()

        # Assert
        assert_equal(tags, set(["namespace_namespace", "namespace_key_value"]))


    @patch('calico_cni.policy_drivers.requests.Session', autospec=True)
    @patch('json.loads', autospec=True)
    def test_get_api_pod(self, m_json_load, m_session):
        # Set up driver.
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'
        pod1 = '{"metadata": {"namespace": "a", "name": "pod-1"}}'

        api_root = "http://kubernetesapi:8080/api/v1/"
        self.driver.api_root = api_root

        # Set up mock objects
        self.driver.auth_token = 'TOKEN'

        get_obj = Mock()
        get_obj.status_code = 200
        get_obj.text = pod1

        m_session_obj = Mock()
        m_session_obj.headers = Mock()
        m_session_obj.get.return_value = get_obj

        m_session.return_value = m_session_obj
        m_session_obj.__enter__ = Mock(return_value=m_session_obj)
        m_session_obj.__exit__ = Mock(return_value=False)

        # Call method under test
        self.driver._get_api_pod()

        # Assert correct data in calls.
        m_session_obj.headers.update.assert_called_once_with(
            {'Authorization': 'Bearer ' + 'TOKEN'})
        m_session_obj.get.assert_called_once_with(
            api_root + 'namespaces/a/pods/pod-1',
            verify=False)
        m_json_load.assert_called_once_with(pod1)

    @patch("calico_cni.policy_drivers.HTTPClient", autospec=True)
    @patch("calico_cni.policy_drivers.Query", autospec=True)
    @patch("calico_cni.policy_drivers.KubeConfig", autospec=True)
    def test_get_api_pod_kubeconfig(self, m_kcfg, m_query, m_http):
        # Set up driver.
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'

        pod = Mock()
        pod.obj = '{"metadata": {"namespace": "a", "name": "pod-1"}}'
        m_query(1, 2, 3).get_by_name.return_value = pod

        api_root = "http://kubernetesapi:8080/api/v1/"
        self.driver.api_root = api_root
        self.driver.kubeconfig_path = "/path/to/kubeconfig"

        # Call method under test
        p = self.driver._get_api_pod()

        # Assert
        assert_equal(p, pod.obj)

    @patch("calico_cni.policy_drivers.HTTPClient", autospec=True)
    @patch("calico_cni.policy_drivers.Query", autospec=True)
    @patch("calico_cni.policy_drivers.KubeConfig", autospec=True)
    def test_get_api_pod_kubeconfig_error(self, m_kcfg, m_query, m_http):
        # Set up driver.
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'

        pod = Mock()
        pod.obj = '{"metadata": {"namespace": "a", "name": "pod-1"}}'
        m_query(1, 2, 3).get_by_name.side_effect = KeyError

        api_root = "http://kubernetesapi:8080/api/v1/"
        self.driver.api_root = api_root
        self.driver.kubeconfig_path = "/path/to/kubeconfig"

        # Call method under test
        with assert_raises(PolicyException) as err:
            self.driver._get_api_pod()

    @patch('calico_cni.policy_drivers.requests.Session', autospec=True)
    @patch('json.loads', autospec=True)
    def test_get_api_pod_with_client_certs(self, m_json_load, m_session):
        # Set up driver.
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'
        pod1 = '{"metadata": {"namespace": "a", "name": "pod-1"}}'

        api_root = "http://kubernetesapi:8080/api/v1/"
        self.driver.api_root = api_root
        self.driver.client_certificate = "cert.pem"
        self.driver.client_key = "key.pem"
        self.driver.certificate_authority = "ca.pem"

        get_obj = Mock()
        get_obj.status_code = 200
        get_obj.text = pod1

        m_session_obj = Mock()
        m_session_obj.headers = Mock()
        m_session_obj.get.return_value = get_obj

        m_session.return_value = m_session_obj
        m_session_obj.__enter__ = Mock(return_value=m_session_obj)
        m_session_obj.__exit__ = Mock(return_value=False)

        # Call method under test
        self.driver._get_api_pod()

        # Assert correct data in calls.
        m_session_obj.get.assert_called_once_with(
            api_root + 'namespaces/a/pods/pod-1',
            verify="ca.pem", cert=("cert.pem", "key.pem"))
        m_json_load.assert_called_once_with(pod1)

    @patch('calico_cni.policy_drivers.requests.Session', autospec=True)
    @patch('json.loads', autospec=True)
    def test_get_pod_config_error(self, m_json_load, m_session):
        """Test _get_api_path with API Access Error
        """
        # Set up mock objects
        self.driver.auth_token = 'TOKEN'

        m_session_obj = Mock()
        m_session_obj.headers = Mock()
        m_session_obj.get.side_effect = BaseException

        m_session.return_value = m_session_obj
        m_session_obj.__enter__ = Mock(return_value=m_session_obj)
        m_session_obj.__exit__ = Mock(return_value=False)

        # Call method under test
        assert_raises(ApplyProfileError, self.driver._get_api_pod)

    @patch('calico_cni.policy_drivers.requests.Session', autospec=True)
    @patch('json.loads', autospec=True)
    def test_get_pod_config_response_code(self, m_json_load, m_session):
        """Test _get_api_path with incorrect status_code
        """
        # Set up class member
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'
        pod1 = '{"metadata": {"namespace": "a", "name": "pod-1"}}'

        # Set up mock objects
        self.driver.auth_token = 'TOKEN'

        get_obj = Mock()
        get_obj.status_code = 404

        m_session_obj = Mock()
        m_session_obj.headers = Mock()
        m_session_obj.get.return_value = get_obj

        m_session.return_value = m_session_obj
        m_session_obj.__enter__ = Mock(return_value=m_session_obj)
        m_session_obj.__exit__ = Mock(return_value=False)

        # Call method under test
        assert_raises(ApplyProfileError, self.driver._get_api_pod)

    @patch('calico_cni.policy_drivers.requests.Session', autospec=True)
    @patch('json.loads', autospec=True)
    def test_get_api_pod_parse_error(self, m_json_load, m_session):
        # Set up driver.
        self.driver.pod_name = 'pod-1'
        self.driver.namespace = 'a'
        pod1 = '{"metadata": {"namespace": "a", "name": "pod-1"}}'

        api_root = "http://kubernetesapi:8080/api/v1/"
        self.driver.api_root = api_root

        # Set up mock objects
        self.driver.auth_token = 'TOKEN'

        m_json_load.side_effect = TypeError

        get_obj = Mock()
        get_obj.status_code = 200
        get_obj.text = pod1

        m_session_obj = Mock()
        m_session_obj.headers = Mock()
        m_session_obj.get.return_value = get_obj

        m_session.return_value = m_session_obj
        m_session_obj.__enter__ = Mock(return_value=m_session_obj)
        m_session_obj.__exit__ = Mock(return_value=False)

        # Call method under test
        assert_raises(ApplyProfileError, self.driver._get_api_pod)

    def test_get_metadata_missing(self):
        # Mock out self.pod in the driver.
        self.driver.pod = {}

        # Attempt to get metadata
        annotations = self.driver._get_metadata("annotations")

        # Should be None
        assert_equal(annotations, None)


class KubernetesPolicyDriverTest(unittest.TestCase):
    """
    Test class for DefaultDenyInboundDriver class.
    """
    def setUp(self):
        self.network_name = "net-name"
        self.namespace = "default"
        self.driver = KubernetesPolicyDriver(self.network_name, self.namespace,
                                             None, None, None, None, None, None)

        # Mock the DatastoreClient
        self.client = MagicMock(spec=DatastoreClient)
        self.driver._client = self.client

    def test_apply_profile(self):
        endpoint = MagicMock(spec=Endpoint)
        endpoint.endpoint_id = "12345"

        # Mock out the k8s API call.
        self.driver._get_api_pod = Mock(spec=self.driver._get_api_pod)
        self.driver._get_api_pod.return_value = \
            {"metadata": {"labels": {"label1": "labelval"}}}

        # Call
        self.driver.apply_profile(endpoint)

        # Assert
        self.client.update_endpoint.assert_called_once_with(endpoint)


    def test_remove_profile(self):
        """
        Should do nothing.
        """
        self.driver.remove_profile()


class GetPolicyDriverTest(unittest.TestCase):

    def test_get_policy_driver_default_k8s(self):
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = {"name": "testnetwork"}
        cni_plugin.k8s_pod_name = "podname"
        cni_plugin.k8s_namespace = "namespace"
        cni_plugin.running_under_k8s = True
        cni_plugin.running_under_mesos = False
        driver = get_policy_driver(cni_plugin)
        assert_true(isinstance(driver, KubernetesNoPolicyDriver))

    def test_get_policy_driver_k8s_annotations(self):
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = {"name": "testnetwork",
                                     "policy": {"type": "k8s-annotations"}}
        cni_plugin.k8s_pod_name = "podname"
        cni_plugin.k8s_namespace = "namespace"
        cni_plugin.running_under_k8s = True
        cni_plugin.running_under_mesos = False
        driver = get_policy_driver(cni_plugin)
        assert_true(isinstance(driver, KubernetesAnnotationDriver))

    def test_get_policy_driver_k8s(self):
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = {"name": "testnetwork", "policy":{"type": "k8s"}}
        cni_plugin.k8s_pod_name = "podname"
        cni_plugin.k8s_namespace = "namespace"
        cni_plugin.running_under_k8s = True
        cni_plugin.running_under_mesos = False
        driver = get_policy_driver(cni_plugin)
        assert_true(isinstance(driver, KubernetesPolicyDriver))

    def test_get_unknown_policy_driver(self):
        config = {"name": "n", "policy": {"type": "madeup"}}
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = config
        with assert_raises(SystemExit) as err:
            get_policy_driver(cni_plugin)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_missing_cert(self):
        config = {"name": "n", "policy": {"type": "k8s", "k8s_client_certificate":"surely this can't exist?"}}
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = config
        cni_plugin.running_under_k8s = True
        cni_plugin.running_under_mesos = False
        cni_plugin.k8s_pod_name = "podname"
        cni_plugin.k8s_namespace = "namespace"
        with assert_raises(SystemExit) as err:
            get_policy_driver(cni_plugin)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    @patch("calico_cni.policy_drivers.DefaultPolicyDriver", autospec=True)
    def test_get_policy_driver_value_error(self, m_driver):
        # Mock
        m_driver.side_effect = ValueError
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = {"name": "testnetwork"}
        cni_plugin.running_under_k8s = False
        cni_plugin.running_under_mesos = False

        # Call
        with assert_raises(SystemExit) as err:
            get_policy_driver(cni_plugin)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_get_policy_driver_mesos(self):
        cni_plugin = Mock(spec=CniPlugin)
        cni_plugin.network_config = {"name": "testnetwork",
                                     "policy":{"type": "k8s"},
                                     "args": {
                                         "org.apache.mesos": {}
                                       }
                                     }
        cni_plugin.running_under_k8s = False
        cni_plugin.running_under_mesos = True
        driver = get_policy_driver(cni_plugin)
        assert_true(isinstance(driver, MesosPolicyDriver))

