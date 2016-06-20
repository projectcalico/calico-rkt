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

from __future__ import print_function
import logging
import json
import yaml
import os
import sys

from docopt import docopt
from subprocess import Popen, PIPE
from netaddr import IPNetwork, AddrFormatError

from pycalico import netns
from pycalico.netns import Namespace, CalledProcessError
from pycalico.datastore import (DatastoreClient, ETCD_AUTHORITY_ENV,
                                ETCD_ENDPOINTS_ENV)
from pycalico.datastore_errors import MultipleEndpointsMatch

from calico_cni.version import __version__, __commit__, __branch__
from calico_cni.util import (configure_logging, parse_cni_args, print_cni_error,
                  handle_datastore_error, CniError)

from calico_cni.container_engines import get_container_engine
from calico_cni.constants import *
from calico_cni.policy_drivers import ApplyProfileError, get_policy_driver
from ipam import IpamPlugin

# Logging configuration.
_log = logging.getLogger("calico_cni")

__doc__ = """
Usage: calico [-vh]

Description:
    Calico CNI plugin.

Options:
    -h --help           Print this message.
    -v --version        Print the plugin version
"""

class CniPlugin(object):
    """
    Class which encapsulates the function of a CNI plugin.
    """
    def __init__(self, network_config, env):
        self._client = DatastoreClient()
        """
        DatastoreClient for access to the Calico datastore.
        """

        # Parse CNI_ARGS into dictionary so we can extract values.
        cni_args = parse_cni_args(env.get(CNI_ARGS_ENV, ""))

        self.k8s_pod_name = cni_args.get(K8S_POD_NAME)
        """
        Name of Kubernetes pod if running under Kubernetes, else None.
        """

        self.k8s_namespace = cni_args.get(K8S_POD_NAMESPACE)
        """
        Name of Kubernetes namespace if running under Kubernetes, else None.
        """

        self.network_config = network_config
        """
        Network config as provided in the CNI network file passed in
        via stdout.
        """

        self.network_name = network_config["name"]
        """
        Name of the network from the provided network config file.
        """

        self.ipam_type = network_config["ipam"]["type"]
        """
        Type of IPAM to use, e.g calico-ipam.
        """

        self.hostname = network_config.get("hostname", socket.gethostname())
        """
        The hostname to register endpoints under.
        """

        self.container_engine = get_container_engine(self.k8s_pod_name)
        """
        Chooses the correct container engine based on the given configuration.
        """

        self.ipam_env = env
        """
        Environment dictionary used when calling the IPAM plugin.
        """

        self.labels = {}
        """
        Label data to assign to this endpoint.  The origin of the labels is orchestrator
        dependent.
        """

        self.args = network_config.get(ARGS_KEY, {})

        # If there Mesos namespaced data, extract any labels that have been specified.
        # Note that Mesos labels are a list of {"key": <key>, "value": <value>}, so pull
        # the key and values and populate the labels dictionary.
        if MESOS_NS_KEY in self.args:
            _log.info("Extracting Mesos namespaced data")
            labels_list = self.args[MESOS_NS_KEY].    \
                          get(MESOS_NETWORK_INFO_KEY, {}). \
                          get(MESOS_LABELS_OUTER_KEY, {}). \
                          get(MESOS_LABELS_KEY, [])
            for label in labels_list:
                self.labels[label["key"]] = label["value"]

        self.command = env[CNI_COMMAND_ENV]
        assert self.command in [CNI_CMD_DELETE, CNI_CMD_ADD], \
                "Invalid CNI command %s" % self.command
        """
        The command to execute for this plugin instance. Required.
        One of:
          - CNI_CMD_ADD
          - CNI_CMD_DELETE
        """

        self.container_id = env[CNI_CONTAINERID_ENV]
        """
        The container's ID in the containerizer. Required.
        """

        self.cni_netns = env[CNI_NETNS_ENV]
        """
        Relative path to the network namespace of this container.
        """

        self.interface = env[CNI_IFNAME_ENV]
        """
        Name of the interface to create within the container.
        """

        self.cni_path = env[CNI_PATH_ENV]
        """
        Path in which to search for CNI plugins.
        """

        self.running_under_k8s = self.k8s_namespace and self.k8s_pod_name
        if self.running_under_k8s:
            self.workload_id = "%s.%s" % (self.k8s_namespace, self.k8s_pod_name)
            self.orchestrator_id = "k8s"
        else:
            self.workload_id = self.container_id
            self.orchestrator_id = "cni"
        """
        Configure orchestrator specific settings.
        workload_id: In Kubernetes, this is the pod's namespace and name.
                     Otherwise, this is the container ID.
        orchestrator_id: Either "k8s" or "cni".
        """

        # Ensure that the ipam_env CNI_ARGS contains the IgnoreUnknown=1 option
        # See https://github.com/appc/cni/pull/158
        # And https://github.com/appc/cni/pull/127
        self.ipam_env[CNI_ARGS_ENV] = 'IgnoreUnknown=1'
        if env.get(CNI_ARGS_ENV):
            # Append any existing args - if they are set.
            self.ipam_env[CNI_ARGS_ENV] += ";%s" % env.get(CNI_ARGS_ENV)

        self.policy_driver = get_policy_driver(self)
        """
        Chooses the correct policy driver based on the given configuration
        """

    def execute(self):
        """
        Execute the CNI plugin - uses the given CNI_COMMAND to determine
        which action to take.

        :return: None.
        """
        if self.command == CNI_CMD_ADD:
            self.add()
        else:
            self.delete()

    def add(self):
        """"Handles CNI_CMD_ADD requests.

        Configures Calico networking and prints required json to stdout.

        In CNI, a container can be added to multiple networks, in which case
        the CNI plugin will be called multiple times.  In Calico, each network
        is represented by a profile, and each container only receives a single
        endpoint / veth / IP address even when it is on multiple CNI networks.

        :return: None.
        """
        # If this container uses host networking, don't network it.
        # This should only be hit when running in Kubernetes mode with
        # docker - rkt doesn't call plugins when using host networking.
        if self.container_engine.uses_host_networking(self.container_id):
            _log.info("Cannot network container %s since it is configured "
                      "with host networking.", self.container_id)
            sys.exit(0)

        _log.info("Configuring network '%s' for container: %s",
                  self.network_name, self.container_id)

        _log.debug("Checking for existing Calico endpoint")
        endpoint = self._get_endpoint()
        if endpoint and not self.running_under_k8s:
            # We've received a create for an existing container, likely on
            # a new CNI network.  We don't need to configure the veth or
            # assign IP addresses, we simply need to add to the new
            # CNI network.  Kubernetes handles this case
            # differently (see below).
            _log.info("Endpoint for container exists - add to new network")
            output = self._add_existing_endpoint(endpoint)
        elif endpoint and self.running_under_k8s:
            # Running under Kubernetes and we've received a create for
            # an existing workload.  Kubernetes only supports a single CNI
            # network, which means that the old pod has been destroyed
            # under our feet and we need to set up networking on the new one.
            # We should also clean up any stale endpoint / IP assignment.
            _log.info("Kubernetes pod has been recreated")
            self._remove_stale_endpoint(endpoint)

            # Release any previous IP addresses assigned to this workload.
            self.ipam_env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(self.ipam_env)

            # Clean up any profiles for the stale endpoint
            self.policy_driver.remove_profile()

            # Configure the new workload.
            self.ipam_env[CNI_COMMAND_ENV] = CNI_CMD_ADD
            output = self._add_new_endpoint()
        else:
            # No endpoint exists - we need to configure a new one.
            _log.info("No endpoint exists for workload - creating")
            output = self._add_new_endpoint()

        # If all successful, print the IPAM plugin's output to stdout.
        dump = json.dumps(output)
        _log.debug("Printing CNI result to stdout: %s", dump)
        print(dump)

        _log.info("Finished networking container: %s", self.container_id)

    def _add_new_endpoint(self):
        """
        Handled adding a new container to a Calico network.
        """
        # Assign IP addresses using the given IPAM plugin.
        _log.info("Configuring a new Endpoint")
        ipv4, ipv6, ipam_result = self._assign_ips(self.ipam_env)

        # Filter out addresses that didn't get assigned.
        ip_list = [ip for ip in [ipv4, ipv6] if ip is not None]

        # Create the Calico endpoint object.
        endpoint = self._create_endpoint(ip_list)

        # Provision the veth for this endpoint.
        endpoint = self._provision_veth(endpoint)

        # Provision / apply profile on the created endpoint.
        try:
            self.policy_driver.apply_profile(endpoint)
        except ApplyProfileError as e:
            _log.error("Failed to apply profile to endpoint %s",
                       endpoint.name)
            self._remove_veth(endpoint)
            self._remove_workload()
            self.ipam_env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(self.ipam_env)
            print_cni_error(ERR_CODE_GENERIC, e.message, e.details)
            sys.exit(ERR_CODE_GENERIC)

        # Return the IPAM plugin's result.
        return ipam_result

    def _add_existing_endpoint(self, endpoint):
        """
        Handles adding an existing container to a new Calico network.

        We've already assigned an IP address and created the veth,
        we just need to apply a new profile to this endpoint.
        """
        # Get the already existing IP information for this Endpoint.
        try:
            ip4 = next(iter(endpoint.ipv4_nets))
        except StopIteration:
            # No IPv4 address on this endpoint.
            _log.warning("No IPV4 address attached to existing endpoint")
            ip4 = IPNetwork("0.0.0.0/32")

        try:
            ip6 = next(iter(endpoint.ipv6_nets))
        except StopIteration:
            # No IPv6 address on this endpoint.
            _log.warning("No IPV6 address attached to existing endpoint")
            ip6 = IPNetwork("::/128")

        # Apply a new profile to this endpoint.
        try:
            self.policy_driver.apply_profile(endpoint)
        except ApplyProfileError as e:
            # Hit an exception applying the profile.  We haven't configured
            # anything, so we don't need to clean anything up.  Just exit.
            _log.error("Failed to apply profile to endpoint %s",
                       endpoint.name)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        return {"ip4": {"ip": str(ip4.cidr)},
                "ip6": {"ip": str(ip6.cidr)}}

    def delete(self):
        """Handles CNI_CMD_DELETE requests.

        Remove this container from Calico networking.

        :return: None.
        """
        _log.info("Remove network '%s' from container: %s",
                self.network_name, self.container_id)

        # Step 1: Remove any IP assignments.
        self._release_ip(self.ipam_env)

        # Step 2: Get the Calico endpoint for this workload. If it does not
        # exist, log a warning and exit successfully.
        endpoint = self._get_endpoint()
        if not endpoint:
            _log.warning("No Calico Endpoint for workload: %s",
                         self.workload_id)
            sys.exit(0)

        # Step 3: Delete the veth interface for this endpoint.
        self._remove_veth(endpoint)

        # Step 4: Delete the Calico workload.
        self._remove_workload()

        # Step 5: Delete any profiles for this endpoint
        self.policy_driver.remove_profile()

        _log.info("Finished removing container: %s", self.container_id)

    def _assign_ips(self, env):
        """Assigns and returns an IPv4 address using the IPAM plugin
        specified in the network config file.

        :return: ipv4, ipv6 - The IP addresses assigned by the IPAM plugin.
        """
        # Call the IPAM plugin.  Returns the plugin returncode,
        # as well as the CNI result from stdout.
        _log.debug("Assigning IP address")
        assert env[CNI_COMMAND_ENV] == CNI_CMD_ADD
        rc, result = self._call_ipam_plugin(env)

        try:
            # Load the response - either the assigned IP addresses or
            # a CNI error message.
            ipam_result = json.loads(result)
        except ValueError:
            message = "Failed to parse IPAM response, exiting"
            _log.exception(message)
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        if rc:
            # The IPAM plugin failed to assign an IP address. At this point in
            # execution, we haven't done anything yet, so we don't have to
            # clean up.
            _log.error("IPAM plugin error (rc=%s): %s", rc, result)
            code = ipam_result.get("code", ERR_CODE_GENERIC)
            msg = ipam_result.get("msg", "Unknown IPAM error")
            details = ipam_result.get("details")
            print_cni_error(code, msg, details)
            sys.exit(int(code))

        try:
            ipv4 = IPNetwork(ipam_result["ip4"]["ip"])
            _log.info("IPAM plugin assigned IPv4 address: %s", ipv4)
        except KeyError:
            ipv4 = None
        except (AddrFormatError, ValueError):
            message = "Invalid or Empty IPv4 address: %s" % \
                      (ipam_result["ip4"]["ip"])
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        try:
            ipv6 = IPNetwork(ipam_result["ip6"]["ip"])
            _log.info("IPAM plugin assigned IPv6 address: %s", ipv6)
        except KeyError:
            ipv6 = None
        except (AddrFormatError, ValueError):
            message = "Invalid or Empty IPv6 address: %s" % \
                      (ipam_result["ip6"]["ip"])
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        if not ipv4 and not ipv6:
            message = "IPAM plugin did not return any valid addresses."
            _log.warning("Bad IPAM plugin response: %s", ipam_result)
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        return ipv4, ipv6, ipam_result

    def _release_ip(self, env):
        """Releases the IP address(es) for this container using the IPAM plugin
        specified in the network config file.

        :param env - A dictionary of environment variables to pass to the
        IPAM plugin
        :return: None.
        """
        _log.info("Releasing IP address")
        assert env[CNI_COMMAND_ENV] == CNI_CMD_DELETE
        rc, _ = self._call_ipam_plugin(env)

        if rc:
            _log.error("IPAM plugin failed to release IP address")

    def _call_ipam_plugin(self, env):
        """
        Executes a CNI IPAM plugin.  If `calico-ipam` is the provided IPAM
        type, then calls directly into ipam.py as a performance optimization.

        For all other types of IPAM, searches the CNI_PATH for the
        correct binary and executes it.

        :return: Tuple of return code, response from the IPAM plugin.
        """
        if self.ipam_type == "calico-ipam":
            _log.info("Using Calico IPAM")
            try:
                response = IpamPlugin(env,
                                      self.network_config["ipam"]).execute()
                code = 0
            except CniError as e:
                # We hit a CNI error - return the appropriate CNI formatted
                # error dictionary.
                response = json.dumps({"code": e.code,
                                       "msg": e.msg,
                                       "details": e.details})
                code = e.code
        else:
            _log.debug("Using binary plugin")
            code, response = self._call_binary_ipam_plugin(env)

        # Return the IPAM return code and output.
        _log.debug("IPAM response (rc=%s): %s", code, response)
        return code, response

    def _call_binary_ipam_plugin(self, env):
        """Calls through to the specified IPAM plugin binary.

        Utilizes the IPAM config as specified in the CNI network
        configuration file.  A dictionary with the following form:
            {
              type: <IPAM TYPE>
            }

        :param env - A dictionary of environment variables to pass to the
        IPAM plugin
        :return: Tuple of return code, response from the IPAM plugin.
        """
        # Find the correct plugin based on the given type.
        plugin_path = self._find_ipam_plugin()
        if not plugin_path:
            message = "Could not find IPAM plugin of type %s in path %s." % \
                      (self.ipam_type, self.cni_path)
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        # Execute the plugin and return the result.
        _log.info("Using IPAM plugin at: %s", plugin_path)
        _log.debug("Passing in environment to IPAM plugin: \n%s",
                   json.dumps(env, indent=2))
        p = Popen(plugin_path, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)
        stdout, stderr = p.communicate(json.dumps(self.network_config))
        _log.debug("IPAM plugin return code: %s", p.returncode)
        _log.debug("IPAM plugin output: \nstdout:\n%s\nstderr:\n%s",
                   stdout, stderr)
        return p.returncode, stdout

    def _create_endpoint(self, ip_list):
        """Creates an endpoint in the Calico datastore with the client.

        :param ip_list - list of IP addresses that have been already allocated
        :return Calico endpoint object
        """
        _log.debug("Creating Calico endpoint with workload_id=%s",
                   self.workload_id)
        try:
            endpoint = self._client.create_endpoint(self.hostname,
                                                    self.orchestrator_id,
                                                    self.workload_id,
                                                    ip_list)
        except (AddrFormatError, KeyError) as e:
            # AddrFormatError: Raised when an IP address type is not
            #                  compatible with the node.
            # KeyError: Raised when BGP config for host is not found.
            _log.exception("Failed to create Calico endpoint.")
            self.ipam_env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(self.ipam_env)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        # Assign labels to the new endpoint object
        _log.debug("Setting Labels: %s", self.labels)
        endpoint.labels = self.labels

        _log.info("Created Calico endpoint with IP address(es) %s", ip_list)
        return endpoint

    def _remove_stale_endpoint(self, endpoint):
        """
        Removes the given endpoint from Calico.
        Called when we discover a stale endpoint that is no longer in use.
        Note that this doesn't release IP allocations - that must be done
        using the designated IPAM plugin.
        """
        _log.info("Removing stale Calico endpoint '%s'", endpoint)
        try:
            self._client.remove_endpoint(endpoint)
        except KeyError:
            # Shouldn't hit this since we know the workload exists.
            _log.info("Error removing stale endpoint, ignoring")

    def _remove_workload(self):
        """Removes the given endpoint from the Calico datastore

        :return: None
        """
        try:
            _log.info("Removing Calico workload '%s'", self.workload_id)
            self._client.remove_workload(hostname=self.hostname,
                                         orchestrator_id=self.orchestrator_id,
                                         workload_id=self.workload_id)
        except KeyError:
            # Attempt to remove the workload using the container ID as the
            # workload ID.  Earlier releases of the plugin used the
            # container ID for the workload ID rather than the Kubernetes pod
            # name and namespace.
            _log.debug("Could not find workload with workload ID %s.",
                         self.workload_id)
            try:
                self._client.remove_workload(hostname=self.hostname,
                                             orchestrator_id="cni",
                                             workload_id=self.container_id)
            except KeyError:
                _log.warning("Could not find workload with container ID %s.",
                             self.container_id)


    def _provision_veth(self, endpoint):
        """Provisions veth for given endpoint.

        Uses the netns relative path passed in through CNI_NETNS_ENV and
        interface passed in through CNI_IFNAME_ENV.

        :param endpoint
        :return Calico endpoint object
        """
        _log.debug("Provisioning Calico veth interface")
        netns_path = os.path.abspath(os.path.join(os.getcwd(), self.cni_netns))
        _log.debug("netns path: %s", netns_path)

        try:
            endpoint.mac = endpoint.provision_veth(
                Namespace(netns_path), self.interface)
        except CalledProcessError as e:
            _log.exception("Failed to provision veth interface for endpoint %s",
                           endpoint.name)
            self._remove_workload()
            self.ipam_env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(self.ipam_env)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        _log.debug("Endpoint has mac address: %s", endpoint.mac)

        self._client.set_endpoint(endpoint)
        _log.info("Provisioned %s in netns %s", self.interface, netns_path)
        return endpoint

    def _remove_veth(self, endpoint):
        """Remove the veth from given endpoint.

        Handles any errors encountered while removing the endpoint.
        """
        _log.info("Removing veth for endpoint: %s", endpoint.name)
        try:
            removed = netns.remove_veth(endpoint.name)
            _log.debug("Successfully removed endpoint %s? %s",
                       endpoint.name, removed)
        except CalledProcessError:
            _log.warning("Unable to remove veth %s", endpoint.name)

    @handle_datastore_error
    def _get_endpoint(self):
        """Get endpoint matching self.workload_id.

        If we cannot find an endpoint using self.workload_id, try
        using self.container_id.

        Return None if no endpoint is found.
        Exits with an error if multiple endpoints are found.

        :return: Endpoint object if found, None if not found
        """
        try:
            _log.debug("Looking for endpoint that matches workload ID %s",
                       self.workload_id)
            endpoint = self._client.get_endpoint(
                hostname=self.hostname,
                orchestrator_id=self.orchestrator_id,
                workload_id=self.workload_id
            )
        except KeyError:
            # Try to find using the container ID.  In earlier version of the
            # plugin, the container ID was used as the workload ID.
            _log.debug("No endpoint found matching workload ID %s",
                       self.workload_id)
            try:
                endpoint = self._client.get_endpoint(
                    hostname=self.hostname,
                    orchestrator_id="cni",
                    workload_id=self.container_id
                )
            except KeyError:
                # We were unable to find an endpoint using either the
                # workload ID or the container ID.
                _log.debug("No endpoint found matching container ID %s",
                           self.container_id)
                endpoint = None
        except MultipleEndpointsMatch:
            message = "Multiple Endpoints found matching ID %s" % \
                    self.workload_id
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        return endpoint

    def _find_ipam_plugin(self):
        """Locates IPAM plugin binary in plugin path and returns absolute path
        of plugin if found; if not found returns an empty string.

        IPAM plugin type is set in the network config file.
        The plugin path is the CNI path passed through the environment variable
        CNI_PATH.

        :rtype : str
        :return: plugin_path - absolute path of IPAM plugin binary
        """
        plugin_type = self.ipam_type
        plugin_path = ""
        for path in self.cni_path.split(":"):
            _log.debug("Looking for plugin %s in path %s", plugin_type, path)
            temp_path = os.path.abspath(os.path.join(path, plugin_type))
            if os.path.isfile(temp_path):
                _log.debug("Found plugin %s in path %s", plugin_type, path)
                plugin_path = temp_path
                break
        return str(plugin_path)


def main():
    """
    Main function - configures and runs the plugin.
    """
    # Read the network config file from stdin. Replace newline characters
    # so that we can properly load it as json.
    # The python json library loads strings as as the unicode type. This causes
    # problems when loading values into os.environ.
    # Rather than try to encode them as strings, just use the yaml library.
    # For more details see http://stackoverflow.com/questions/956867/how-to-get-string-objects-instead-of-unicode-ones-from-json-in-python
    config_raw = ''.join(sys.stdin.readlines()).replace('\n', '').replace('\t','')
    network_config = yaml.safe_load(config_raw)

    # Get the log level from the config file, default to INFO.
    log_level_file = network_config.get(LOG_LEVEL_FILE_KEY, "INFO").upper()
    log_level_stderr = network_config.get(LOG_LEVEL_STDERR_KEY, "ERROR").upper()

    log_filename = "cni.log"
    # Configure logging for CNI
    configure_logging(_log, log_level_file, log_level_stderr, log_filename)

    # Configure logging for libcalico (pycalico)
    configure_logging(logging.getLogger("pycalico"), "WARNING", "WARNING",
                      log_filename)

    _log.debug("Loaded network config:\n%s",
               json.dumps(network_config, indent=2))

    # Get the etcd configuration from the config file. Set the
    # environment variables.
    etcd_authority = network_config.get(ETCD_AUTHORITY_KEY)
    etcd_endpoints = network_config.get(ETCD_ENDPOINTS_KEY)
    if etcd_authority:
        os.environ[ETCD_AUTHORITY_ENV] = etcd_authority
        _log.debug("Using %s=%s", ETCD_AUTHORITY_ENV, etcd_authority)
    if etcd_endpoints:
        os.environ[ETCD_ENDPOINTS_ENV] = etcd_endpoints
        _log.debug("Using %s=%s", ETCD_ENDPOINTS_ENV, etcd_endpoints)

    # Get the CNI environment.
    env = os.environ.copy()
    _log.debug("Loaded environment:\n%s", json.dumps(env, indent=2))

    # Call the CNI plugin and handle any errors.
    rc = 0
    try:
        _log.info("Starting Calico CNI plugin execution")
        CniPlugin(network_config, env).execute()
    except SystemExit as e:
        # SystemExit indicates an error that was handled earlier
        # in the stack.  Just set the return code.
        rc = e.code
    except Exception:
        # An unexpected Exception has bubbled up - catch it and
        # log it out.
        _log.exception("Unhandled Exception killed plugin")
        rc = ERR_CODE_GENERIC
        print_cni_error(rc, "Unhandled Exception killed plugin")
    finally:
        _log.info("Calico CNI execution complete, rc=%s", rc)
        sys.exit(rc)


if __name__ == '__main__': # pragma: no cover
    # Parse out the provided arguments.
    command_args = docopt(__doc__)

    # If the version argument was given, print version and exit.
    if command_args.get("--version"):
        print(json.dumps({"Version": __version__,
                          "Commit": __commit__,
                          "Branch": __branch__}, indent=2))
        sys.exit(0)

    try:
        main()
    except Exception as e:
        # Catch any unhandled exceptions in the main() function.  Any errors
        # in CniPlugin.execute() are already handled.
        print_cni_error(ERR_CODE_GENERIC,
                        "Unhandled Exception in main()",
                        e.message)
        sys.exit(ERR_CODE_GENERIC)
