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

import json
import unittest

from mock import patch, MagicMock, ANY, call
from netaddr import IPNetwork
from nose.tools import assert_equal, assert_false, assert_raises
from nose_parameterized import parameterized
from pycalico.datastore import DatastoreClient
from pycalico.datastore_datatypes import Endpoint
from pycalico.datastore_errors import MultipleEndpointsMatch
from subprocess32 import CalledProcessError, Popen, PIPE

from calico_cni.constants import *
from calico_cni.policy_drivers import (DefaultPolicyDriver, ApplyProfileError)
from calico_cni.container_engines import DockerEngine 
from calico_cni.util import CniError
from calico import main, CniPlugin


class CniPluginTest(unittest.TestCase):
    """
    Test class for CniPlugin class.
    """
    def setUp(self):
        """
        Per-test setup method.
        """
        self.container_id = "ff3afbd1-17ad-499d-b514-72438c009e81"
        self.network_config = {
            "name": "ut-network",
            "etcd_authority": "dummy_value:1234",
            "etcd_endpoints": "http://dummy_value:1234",
            "type": "calico",
            "hostname": "test-host",
            "ipam": {
                "type": "calico-ipam",
                "subnet": "10.22.0.0/16",
                "routes": [{"dst": "0.0.0.0/0"}],
                "range-start": "",
                "range-end": ""
            },
            "labels": {
                "group": "production"
            }
        }
        self.env = {
                CNI_CONTAINERID_ENV: self.container_id,
                CNI_IFNAME_ENV: "eth0",
                CNI_ARGS_ENV: "IP=1.2.3.4",
                CNI_COMMAND_ENV: CNI_CMD_ADD, 
                CNI_PATH_ENV: "/usr/bin/rkt/",
                CNI_NETNS_ENV: "netns",
        }

        # Create the CniPlugin to test.
        self.plugin = CniPlugin(self.network_config, self.env)

        # Mock out policy driver. 
        self.plugin.policy_driver = MagicMock(spec=DefaultPolicyDriver)

        # Mock out the datastore client.
        self.m_datastore_client = MagicMock(spec=DatastoreClient)
        self.plugin._client = self.m_datastore_client

        # Set expected values.
        self.expected_orch_id = "cni"
        self.expected_workload_id = self.container_id

    def test_execute_add_mainline(self):
        """Test execute() ADD

        Set command to add, assert add() called.
        """
        self.plugin.command = CNI_CMD_ADD
        self.plugin.add = MagicMock(self.plugin.add)
        self.plugin.execute()
        self.plugin.add.assert_called_once_with()

    def test_execute_del_mainline(self):
        """Test execute() DEL

        Set command to delete, assert delete() called.
        """
        self.plugin.command = CNI_CMD_DELETE
        self.plugin.delete = MagicMock(self.plugin.delete)
        self.plugin.execute()
        self.plugin.delete.assert_called_once_with()

    @patch("calico.json", autospec=True)
    def test_add_mainline(self, m_json): 
        # Mock out _assign_ips.
        ip4 = IPNetwork("10.0.0.1/32")
        ip6 = IPNetwork("0:0:0:0:0:ffff:a00:1/128")
        ipam_response = json.dumps({"ip4": {"ip": str(ip4.cidr)},
                                    "ip6": {"ip": str(ip6.cidr)}})
        self.plugin._assign_ips = MagicMock(spec=self.plugin._assign_ips)
        self.plugin._assign_ips.return_value = ip4, ip6, ipam_response

        # Mock out _create_endpoint.
        endpoint = MagicMock(spec=Endpoint)
        self.plugin._create_endpoint = MagicMock(spec=self.plugin._create_endpoint)
        self.plugin._create_endpoint.return_value = endpoint

        # Mock out _provision_veth. 
        self.plugin._provision_veth = MagicMock(spec=self.plugin._provision_veth)
        self.plugin._provision_veth.return_value = endpoint

        # Mock out _get_endpoint - no endpoint exists.
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = None

        # Call method.
        self.plugin.add()

        # Assert.
        self.plugin._assign_ips.assert_called_once_with(self.plugin.ipam_env)
        self.plugin._create_endpoint.assert_called_once_with([ip4, ip6])
        self.plugin._provision_veth.assert_called_once_with(endpoint)
        self.plugin.policy_driver.apply_profile.assert_called_once_with(endpoint)
        m_json.dumps.assert_called_once_with(ipam_response)

    @patch("calico.json", autospec=True)
    def test_add_host_networking(self, m_json): 
        # Mock out.
        self.plugin.container_engine.uses_host_networking = MagicMock(return_value=True)

        # Call method.
        assert_raises(SystemExit, self.plugin.add)

    @patch("calico.json", autospec=True)
    def test_add_exists_new_network(self, m_json): 
        """
        Test add when the endpoint already exists, adding to a new 
        network.
        """
        # Mock out methods that should not be called.
        self.plugin._assign_ips = MagicMock(spec=self.plugin._assign_ips)
        self.plugin._create_endpoint = MagicMock(spec=self.plugin._create_endpoint)
        self.plugin._provision_veth = MagicMock(spec=self.plugin._provision_veth)

        # Mock out _get_endpoint - endpoint exists.
        ip4 = IPNetwork("10.0.0.1")
        ip6 = IPNetwork("bad::beef")
        endpoint = MagicMock(spec=Endpoint)
        endpoint.ipv4_nets = [ip4]
        endpoint.ipv6_nets = [ip6]
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = endpoint

        # Expected response.
        expected = {"ip4": {"ip": str(ip4)}, "ip6": {"ip": str(ip6)}}

        # Call method.
        self.plugin.add()

        # Assert.
        assert_false(self.plugin._assign_ips.called)
        assert_false(self.plugin._create_endpoint.called)
        assert_false(self.plugin._provision_veth.called)
        self.plugin.policy_driver.apply_profile.assert_called_once_with(endpoint)
        m_json.dumps.assert_called_once_with(expected)

    @patch("calico.json", autospec=True)
    def test_add_profile_error(self, m_json): 
        """
        Test add when the endpoint does not exist, error applying profile.
        """
        # Mock out cleanup methods.
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)
        self.plugin._remove_workload = MagicMock(spec=self.plugin._remove_workload)
        self.plugin._remove_veth = MagicMock(spec=self.plugin._remove_veth)

        # Mock out IPAM response.
        ip4 = IPNetwork("10.0.0.1/32")
        ip6 = IPNetwork("0:0:0:0:0:ffff:a00:1/128")
        ipam_response = json.dumps({"ip4": {"ip": str(ip4.cidr)},
                                    "ip6": {"ip": str(ip6.cidr)}})

        # Mock out _assign_ips.
        self.plugin._assign_ips = MagicMock(spec=self.plugin._assign_ips)
        self.plugin._assign_ips.return_value = ip4, ip6, ipam_response

        # Mock out _create_endpoint.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.name = "cali12345"
        self.plugin._create_endpoint = MagicMock(spec=self.plugin._create_endpoint)
        self.plugin._create_endpoint.return_value = endpoint

        # Mock out _provision_veth. 
        self.plugin._provision_veth = MagicMock(spec=self.plugin._provision_veth)
        self.plugin._provision_veth.return_value = endpoint

        # Mock out apply_profile to throw error.
        msg = "Apply Profile Error Message"
        error = ApplyProfileError(msg)
        self.plugin.policy_driver.apply_profile.side_effect = error  

        # Mock out _get_endpoint - endpoint exists.
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = None 

        # Call method.
        assert_raises(SystemExit, self.plugin.add)

    @patch("calico.json", autospec=True)
    def test_add_exists_new_network_profile_error(self, m_json): 
        """
        Test add when the endpoint already exists, adding to a new 
        network, error applying profile.
        """
        # Mock out apply_profile to throw error.
        self.plugin.policy_driver.apply_profile.side_effect = ApplyProfileError

        # Mock out _get_endpoint - endpoint exists.
        ip4 = IPNetwork("10.0.0.1")
        ip6 = IPNetwork("bad::beef")
        endpoint = MagicMock(spec=Endpoint)
        endpoint.ipv4_nets = [ip4]
        endpoint.ipv6_nets = [ip6]
        endpoint.name = "cali12345"
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = endpoint

        # Call method.
        assert_raises(SystemExit, self.plugin.add)

    @patch("calico.json", autospec=True)
    def test_add_exists_no_ips(self, m_json): 
        """
        Tests add to new network when endpoint exists,
        no IP addresses are assigned. 
        """
        # Mock out _get_endpoint - endpoint exists.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.ipv4_nets = []
        endpoint.ipv6_nets = []
        endpoint.name = "cali12345"
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = endpoint

        # Call method.
        self.plugin.add()

        # Assert profile add is called.
        self.plugin.policy_driver.apply_profile.assert_called_once_with(endpoint)

    @patch("calico.netns", autospec=True)
    def test_delete_mainline(self, m_netns):
        # Mock out _release_ip.
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)

        # Mock out _get_endpoint.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.name = "cali12345"
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = endpoint

        # Mock out _remove_workload.
        self.plugin._remove_workload = MagicMock(spec=self.plugin._remove_workload)

        # Call delete()
        self.plugin.delete()

        # Assert.
        self.plugin._release_ip.assert_called_once_with(self.plugin.ipam_env)
        self.plugin._get_endpoint.assert_called_once_with()
        self.plugin._remove_workload.assert_called_once_with()
        m_netns.remove_veth.assert_called_once_with("cali12345")
        self.plugin.policy_driver.remove_profile.assert_called_once_with()

    @patch("calico.netns", autospec=True)
    def test_delete_no_endpoint(self, m_netns):
        # Mock out _release_ip.
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)

        # Mock out _remove_workload.
        self.plugin._remove_workload = MagicMock(spec=self.plugin._remove_workload)

        # Mock out _get_endpoint.
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = None  

        # Call delete()
        assert_raises(SystemExit, self.plugin.delete)

        # Assert.
        self.plugin._release_ip.assert_called_once_with(self.plugin.ipam_env)
        self.plugin._get_endpoint.assert_called_once_with()
        assert_false(self.plugin._remove_workload.called)
        assert_false(m_netns.remove_veth.called)

    def test_assign_ip_mainline(self):
        # Mock _call_ipam_plugin.
        ip4 = "10.0.0.1/32"
        ip6 = "0:0:0:0:0:ffff:a00:1"
        rc = 0
        ipam_result = json.dumps({"ip4": {"ip": ip4}, "ip6": {"ip": ip6}})
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ipam_result
        env = {CNI_COMMAND_ENV: CNI_CMD_ADD}

        # Call _assign_ips.
        result = self.plugin._assign_ips(env)

        # Assert.
        assert_equal(result[0], IPNetwork(ip4))
        assert_equal(result[1], IPNetwork(ip6))
        assert_equal(result[2], json.loads(ipam_result))

    def test_assign_ip_invalid_response(self):
        # Mock _call_ipam_plugin.
        rc = 1
        ipam_result = "Invalid json" 
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ipam_result
        env = {CNI_COMMAND_ENV: CNI_CMD_ADD}

        # Call _assign_ips.
        with assert_raises(SystemExit) as err:
            self.plugin._assign_ips(env)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_assign_ip_bad_rc(self):
        # Mock _call_ipam_plugin.
        rc = ERR_CODE_GENERIC
        msg = "Message"
        details = "Details"
        ipam_result = json.dumps({"code": rc, 
                                  "msg": msg, 
                                  "details": details})
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ipam_result
        env = {CNI_COMMAND_ENV: CNI_CMD_ADD}

        # Call _assign_ips.
        with assert_raises(SystemExit) as err:
            self.plugin._assign_ips(env)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_assign_ip_no_ip(self):
        # Mock _call_ipam_plugin.
        rc = 0
        ipam_result = json.dumps({})
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ipam_result
        env = {CNI_COMMAND_ENV: CNI_CMD_ADD}

        # Call _assign_ips.
        with assert_raises(SystemExit) as err:
            self.plugin._assign_ips(env)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    @parameterized.expand(["ip4", "ip6"])
    def test_assign_ip_invalid_ip(self, ip_type):
        """
        Tests handling of invalid IP addresses returned by
        the IPAM plugin.
        """
        # Mock _call_ipam_plugin.
        rc = 0
        ipam_result = json.dumps({ip_type: {"ip": "somebadip"}})
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ipam_result
        env = {CNI_COMMAND_ENV: CNI_CMD_ADD}

        # Call _assign_ips.
        with assert_raises(SystemExit) as err:
            self.plugin._assign_ips(env)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_release_ip_mainline(self):
        # Mock _call_ipam_plugin.
        rc = 0
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ""
        env = {CNI_COMMAND_ENV: CNI_CMD_DELETE}

        # Call _release_ip.
        self.plugin._release_ip(env)

    def test_release_ip_failed(self):
        # Mock _call_ipam_plugin.
        rc = 100
        self.plugin._call_ipam_plugin = MagicMock(spec=self.plugin._call_ipam_plugin)
        self.plugin._call_ipam_plugin.return_value = rc, ""
        env = {CNI_COMMAND_ENV: CNI_CMD_DELETE}

        # Call _release_ip.
        self.plugin._release_ip(env)

    @patch("calico.IpamPlugin", autospec=True)
    def test_call_ipam_plugin_calico_mainline(self, m_ipam_plugin):
        # Mock _find_ipam_plugin.
        plugin_path = "/opt/bin/cni/calico-ipam"
        self.plugin._find_ipam_plugin = MagicMock(spec=self.plugin._find_ipam_plugin)
        self.plugin._find_ipam_plugin.return_value = plugin_path

        # Mock out return values.
        ip4 = "10.0.0.1/32"
        ip6 = "0:0:0:0:0:ffff:a00:1"
        env = {}
        out = json.dumps({"ip4": {"ip": ip4}, "ip6": {"ip": ip6}})
        m_ipam_plugin(env, self.network_config).execute.return_value = out

        # Set IPAM type.
        self.plugin.ipam_type = "calico-ipam"

        # Call _call_ipam_plugin.
        rc, result = self.plugin._call_ipam_plugin(env)

        # Assert.
        assert_equal(rc, 0)
        assert_equal(result, out)

    @patch("calico.IpamPlugin", autospec=True)
    def test_call_ipam_plugin_calico_error(self, m_ipam_plugin):
        # Mock _find_ipam_plugin.
        plugin_path = "/opt/bin/cni/calico-ipam"
        self.plugin._find_ipam_plugin = MagicMock(spec=self.plugin._find_ipam_plugin)
        self.plugin._find_ipam_plugin.return_value = plugin_path

        # Mock out return values.
        env = {}
        err = CniError(150, "message", "details")
        m_ipam_plugin(env, self.network_config).execute.side_effect = err 

        # Set IPAM type.
        self.plugin.ipam_type = "calico-ipam"

        # Call _call_ipam_plugin.
        rc, result = self.plugin._call_ipam_plugin(env)

        # Assert.
        expected = '{"msg": "message", "code": 150, "details": "details"}'
        assert_equal(rc, 150)
        assert_equal(result, expected)

    @patch("calico.Popen", autospec=True)
    def test_call_ipam_plugin_binary_mainline(self, m_popen):
        # Mock _find_ipam_plugin.
        plugin_path = "/opt/bin/cni/calico-ipam"
        self.plugin._find_ipam_plugin = MagicMock(spec=self.plugin._find_ipam_plugin)
        self.plugin._find_ipam_plugin.return_value = plugin_path

        # Mock out return values.
        ip4 = "10.0.0.1/32"
        ip6 = "0:0:0:0:0:ffff:a00:1"
        stdout = json.dumps({"ip4": {"ip": ip4}, "ip6": {"ip": ip6}})
        stderr = ""
        m_proc = MagicMock(spec=Popen)
        m_proc.communicate.return_value = (stdout, stderr)
        m_proc.returncode = 0
        m_popen.return_value = m_proc
        env = {}

        # Set IPAM type.
        self.plugin.ipam_type = "not-calico"

        # Call _call_ipam_plugin.
        rc, result = self.plugin._call_ipam_plugin(env)

        # Assert.
        assert_equal(rc, 0)
        m_popen.assert_called_once_with(plugin_path, 
                                        stdin=PIPE, 
                                        stdout=PIPE, 
                                        stderr=PIPE,
                                        env=env)
        m_proc.communicate.assert_called_once_with(json.dumps(self.plugin.network_config))
        assert_equal(result, stdout)

    @patch("calico.Popen", autospec=True)
    def test_call_ipam_plugin_binary_missing(self, m_popen):
        """
        Unable to find IPAM plugin.
        """
        # Mock _find_ipam_plugin.
        self.plugin._find_ipam_plugin = MagicMock(spec=self.plugin._find_ipam_plugin)
        self.plugin._find_ipam_plugin.return_value = None 
        env = {}

        # Set IPAM type. 
        self.plugin.ipam_type = "not-calico"

        # Call method.
        with assert_raises(SystemExit) as err:
            self.plugin._call_ipam_plugin(env)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

    def test_create_endpoint_mainline(self):
        # Mock.
        ip4 = IPNetwork("10.0.0.1")
        ip_list = [ip4]
        endpoint = MagicMock(spec=Endpoint)
        self.plugin._client.create_endpoint.return_value = endpoint

        # Call.
        ep = self.plugin._create_endpoint(ip_list)

        # Assert.
        self.plugin._client.create_endpoint.assert_called_once_with(ANY, 
                self.expected_orch_id, self.expected_workload_id, ip_list)
        assert_equal(ep, endpoint)
        self.assertEqual(ep.labels, self.network_config['labels'])

    def test_create_endpoint_error(self):
        # Mock.
        ip4 = IPNetwork("10.0.0.1")
        ip_list = [ip4]
        self.plugin._client.create_endpoint.side_effect = KeyError
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)

        # Call.
        with assert_raises(SystemExit) as err:
            self.plugin._create_endpoint(ip_list)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

        expected_env = self.env.copy()
        expected_env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
        self.plugin._release_ip.assert_called_once_with(expected_env)

    def test_remove_workload_mainline(self):
        # Call
        self.plugin._remove_workload()

        # Assert
        self.plugin._client.remove_workload.assert_called_once_with(hostname=ANY,
                workload_id=self.expected_workload_id, 
                orchestrator_id=self.expected_orch_id)

    def test_remove_workload_does_not_exist(self):
        """
        Make sure we handle this case gracefully - no exception raised.
        """
        self.plugin._client.remove_workload.side_effect = KeyError 
        self.plugin._remove_workload()

    @patch("calico.os", autospec=True)
    @patch("calico.Namespace", autospec=True)
    def test_provision_veth_mainline(self, m_ns, m_os):
        # Mock
        endpoint = MagicMock(spec=Endpoint)
        mac = "aa:bb:cc:dd:ee:ff"
        endpoint.provision_veth.return_value = mac
        m_os.path.abspath.return_value = "/path/to/netns"

        # Call method
        self.plugin._provision_veth(endpoint)

        # Assert.
        assert_equal(endpoint.mac, mac)
        m_ns.assert_called_once_with("/path/to/netns")
        endpoint.provision_veth.assert_called_once_with(m_ns("/path/to/netns"), "eth0")
        self.plugin._client.set_endpoint.assert_called_once_with(endpoint)

    @patch("calico.os", autospec=True)
    @patch("calico.Namespace", autospec=True)
    @patch("calico.print_cni_error", autospec=True)
    def test_provision_veth_error(self, m_print, m_ns, m_os):
        # Mock
        endpoint = MagicMock(spec=Endpoint)
        endpoint.name = "cali12345"
        m_os.path.abspath.return_value = "/path/to/netns"
        endpoint.provision_veth.side_effect = CalledProcessError(1, "cmd", 3)

        # Mock out cleanup methods.
        self.plugin._remove_workload = MagicMock(spec=self.plugin._remove_workload)
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)

        # Call method
        with assert_raises(SystemExit) as err:
            self.plugin._provision_veth(endpoint)
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

        # Assert.
        m_ns.assert_called_once_with("/path/to/netns")
        endpoint.provision_veth.assert_called_once_with(m_ns("/path/to/netns"), "eth0")
        assert_false(self.plugin._client.set_endpoint.called)
        self.plugin._remove_workload.assert_called_once_with()
        self.plugin._release_ip.assert_called_once_with(ANY)

    @patch("calico.netns", autospec=True)
    def test_remove_veth_mainline(self, m_netns):
        # Mock
        endpoint = MagicMock(spec=Endpoint)
        endpoint.name = "cali12345"

        # Call
        self.plugin._remove_veth(endpoint)

        # Assert
        m_netns.remove_veth.assert_called_once_with(endpoint.name)

    @patch("calico.netns", autospec=True)
    def test_remove_veth_error(self, m_netns):
        """
        Make sure we handle errors gracefully - don't re-raise.
        """
        # Mock
        endpoint = MagicMock(spec=Endpoint)
        endpoint.name = "cali12345"
        m_netns.remove_veth.side_effect = CalledProcessError(1, "cmd2", 3)

        # Call
        self.plugin._remove_veth(endpoint)

    def test_get_endpoint_mainline(self):
        # Mock
        endpoint = MagicMock(spec=Endpoint)
        self.plugin._client.get_endpoint.return_value = endpoint

        # Call
        ep = self.plugin._get_endpoint()

        # Assert
        assert_equal(ep, endpoint)
        self.plugin._client.get_endpoint.assert_called_once_with(hostname=ANY, 
                orchestrator_id=self.expected_orch_id, 
                workload_id=self.expected_workload_id)

    def test_get_endpoint_no_endpoint(self):
        # Mock
        self.plugin._client.get_endpoint.side_effect = KeyError

        # Call
        ep = self.plugin._get_endpoint()

        # Assert
        assert_equal(ep, None)
        calls = [call(hostname=ANY, orchestrator_id=self.expected_orch_id, 
                      workload_id=self.expected_workload_id),
                 call(hostname=ANY, orchestrator_id="cni", 
                      workload_id=self.container_id)]
        self.plugin._client.get_endpoint.assert_has_calls(calls)

    def test_get_endpoint_multiple_endpoints(self):
        # Mock
        self.plugin._client.get_endpoint.side_effect = MultipleEndpointsMatch 

        # Call
        with assert_raises(SystemExit) as err:
            self.plugin._get_endpoint()
        e = err.exception
        assert_equal(e.code, ERR_CODE_GENERIC)

        # Assert
        self.plugin._client.get_endpoint.assert_called_once_with(hostname=ANY, 
                orchestrator_id=self.expected_orch_id, 
                workload_id=self.expected_workload_id)

    def test_remove_stale_endpoint(self):
        """
        Test removal of endpoints from datastore.
        """
        endpoint = MagicMock(spec=Endpoint)
        self.plugin._remove_stale_endpoint(endpoint)
        self.plugin._client.remove_endpoint.assert_called_once_with(endpoint)

    def test_remove_stale_endpoint_does_not_exist(self):
        """
        Test removal of endpoint from datastore when the endpoint
        does not exist.
        """
        endpoint = MagicMock(spec=Endpoint)
        self.plugin._client.remove_endpoint.side_effect = KeyError

        # Error should not be re-raised.  If the endpoint does not exist,
        # we're happy with that.
        self.plugin._remove_stale_endpoint(endpoint)

    @patch("calico.os", autospec=True)
    def test_find_ipam_plugin(self, m_os):
        # Mock
        m_os.path.isfile.side_effect = iter([False, True])  # Second time returns true.
        m_os.path.join.side_effect = lambda x,y: x + y
        m_os.path.abspath.side_effect = lambda x: x
        self.plugin.cni_path="/opt/bin/cni/:/opt/cni/bin/"

        # Call
        path = self.plugin._find_ipam_plugin()

        # Assert
        assert_equal(path, "/opt/cni/bin/calico-ipam")

    @patch("calico.os", autospec=True)
    @patch("calico.sys", autospec=True)
    @patch("calico.CniPlugin", autospec=True)
    @patch("calico.configure_logging", autospec=True)
    def test_main(self, m_conf_log, m_plugin, m_sys, m_os):
        # Mock
        m_os.environ = self.env
        m_sys.stdin.readlines.return_value = json.dumps(self.network_config)
        m_plugin(self.env, self.network_config).execute.return_value = 0
        m_plugin.reset_mock()

        # Call
        main()

        # Assert
        m_plugin.assert_called_once_with(self.network_config, self.env)
        m_conf_log.assert_called_with(ANY, "WARNING", "WARNING", "cni.log")
        m_sys.exit.assert_called_once_with(0)

    @patch("calico.os", autospec=True)
    @patch("calico.sys", autospec=True)
    @patch("calico.CniPlugin", autospec=True)
    @patch("calico.configure_logging", autospec=True)
    def test_main_sys_exit(self, m_conf_log, m_plugin, m_sys, m_os):
        """Test main() SystemExit handling"""
        # Mock out _execute to throw SystemExit
        m_os.environ = self.env
        m_sys.stdin.readlines.return_value = json.dumps(self.network_config)
        m_plugin(self.env, self.network_config).execute.side_effect = SystemExit(5)
        m_plugin.reset_mock()

        # Call
        main()

        # Assert
        m_sys.exit.assert_called_once_with(5)

    @patch("calico.os", autospec=True)
    @patch("calico.sys", autospec=True)
    @patch("calico.CniPlugin", autospec=True)
    @patch("calico.configure_logging", autospec=True)
    def test_main_unhandled_exception(self, m_conf_log, m_plugin, m_sys, m_os):
        """Test main() unhandled Exception"""
        # Mock out _execute to throw SystemExit
        m_os.environ = self.env
        m_sys.stdin.readlines.return_value = json.dumps(self.network_config)
        m_plugin(self.env, self.network_config).execute.side_effect = Exception
        m_plugin.reset_mock()

        # Call
        main()

        # Assert
        m_sys.exit.assert_called_once_with(ERR_CODE_GENERIC)


class CniPluginKubernetesTest(CniPluginTest):
    """
    Test class for CniPlugin class when running under Kubernetes. Runs all 
    of the CniPluginTest cases with Kubernetes specific parameters specified.
    """
    def setUp(self):
        """
        Per-test setup method.
        """
        # Call superclass.
        CniPluginTest.setUp(self)

        self.container_id = "ff3afbd1-17ad-499d-b514-72438c009e81"
        self.env = {
                CNI_CONTAINERID_ENV: self.container_id,
                CNI_IFNAME_ENV: "eth0",
                CNI_ARGS_ENV: "K8S_POD_NAME=testpod;K8S_POD_NAMESPACE=k8sns",
                CNI_COMMAND_ENV: CNI_CMD_ADD, 
                CNI_PATH_ENV: "/opt/cni/bin",
                CNI_NETNS_ENV: "netns",
        }

        # Create the CniPlugin to test, using Kubernetes specific
        # config.
        self.plugin = CniPlugin(self.network_config, self.env)

        # Mock out policy driver. 
        self.plugin.policy_driver = MagicMock(spec=DefaultPolicyDriver)

        # Mock out container engine 
        self.plugin.container_engine = MagicMock(spec=DockerEngine)
        self.plugin.container_engine.uses_host_networking.return_value = False

        # Mock out the datastore client.
        self.m_datastore_client = MagicMock(spec=DatastoreClient)
        self.plugin._client = self.m_datastore_client

        # Set the expected values.
        self.expected_orch_id = "k8s"
        self.expected_workload_id = "k8sns.testpod" 

    @patch("calico.json", autospec=True)
    @patch("calico.IpamPlugin", autospec=True)
    def test_add_exists_no_ips(self, m_ipam, m_json): 
        """
        In k8s, if an endpoint exists already, we must clean it up.
        """
        # Mock out _get_endpoint - endpoint exists.
        endpoint = MagicMock(spec=Endpoint)
        endpoint.ipv4_nets = []
        endpoint.ipv6_nets = []
        endpoint.name = "cali12345"
        self.plugin._get_endpoint = MagicMock(spec=self.plugin._get_endpoint)
        self.plugin._get_endpoint.return_value = endpoint

        # Mock remove_stale_endpoint.
        self.plugin._remove_stale_endpoint = MagicMock(spec=self.plugin._remove_stale_endpoint)

        # Mock add_new_endpoint.
        self.plugin._add_new_endpoint = MagicMock(spec=self.plugin._add_new_endpoint)

        # Mock releasing of IPs.
        self.plugin._release_ip = MagicMock(spec=self.plugin._release_ip)

        # Call method.
        self.plugin.add()

        # Assert we remove the endpoint.
        self.plugin._remove_stale_endpoint.assert_called_once_with(endpoint)

        # Assert we release IPs.
        self.plugin._release_ip.assert_called_once_with(ANY)

        # Assert we clean up policy.
        self.plugin.policy_driver.remove_profile.assert_called_once_with()
        
        # Assert we add a new endpoint.
        self.plugin._add_new_endpoint.assert_called_once_with()

    @patch("calico.json", autospec=True)
    def test_add_exists_new_network(self, m_json): 
        """
        In k8s, we never add a new network to an existing endpoint.
        """
        pass

    @patch("calico.json", autospec=True)
    def test_add_exists_new_network_profile_error(self, m_json): 
        pass
