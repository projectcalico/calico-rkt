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
import socket
import functools
import logging
import json
import os
import sys

from subprocess import check_output, CalledProcessError, check_call, Popen, PIPE
from netaddr import IPAddress, IPNetwork, AddrFormatError

from pycalico import netns
from pycalico.ipam import IPAMClient, SequentialAssignment
from pycalico.netns import Namespace, remove_veth
from pycalico.datastore_datatypes import Rules, IPPool
from pycalico.datastore import IF_PREFIX, DatastoreClient
from pycalico.datastore_errors import MultipleEndpointsMatch

# Calico Configuration Constants
ETCD_AUTHORITY_ENV = 'ETCD_AUTHORITY'

ORCHESTRATOR_ID = "rkt"
HOSTNAME = socket.gethostname()
NETNS_ROOT = '/var/lib/rkt/pods/run'

# Logging configuration.
LOG_DIR = "/var/log/calico/cni"
LOG_FILENAME = "cni.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILENAME)
_log = logging.getLogger(__name__)

# Constants for accessing environment variables. The following 
# set of variables are required by the CNI spec.
CNI_COMMAND_ENV = "CNI_COMMAND"
CNI_CONTAINERID_ENV = "CNI_CONTAINERID"
CNI_NETNS_ENV = "CNI_NETNS"
CNI_IFNAME_ENV = "CNI_IFNAME"
CNI_ARGS_ENV = "CNI_ARGS"
CNI_PATH_ENV = "CNI_PATH"

# CNI Constants
CNI_CMD_ADD = "ADD"
CNI_CMD_DELETE = "DEL"


class CniPlugin(object):
    """
    Class which encapsulates the function of a CNI 
    plugin.
    """
    def __init__(self, network_config, env):
        self.network_config = network_config
        """
        Network config as provided in the CNI network file passed in
        via stdout.
        """

        self.env = env
        """
        Copy of the environment variable dictionary. Contains CNI_* 
        variables.
        """

        self._client = DatastoreClient()
        """
        DatastoreClient for access to the Calico datastore.
        """

        self.command = env[CNI_COMMAND_ENV]
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

        self.netns = env[CNI_NETNS_ENV]
        """
        Relative path to the network namespace of this container.
        """

        self.interface = env[CNI_IFNAME_ENV]
        """
        Name of the interface to create within the container.
        """

        self.cni_args = self.parse_cni_args(env[CNI_ARGS_ENV])
        """
        Dictionary of additional CNI arguments provided via
        the CNI_ARGS environment variable.
        """

        self.cni_path = env[CNI_PATH_ENV]
        """
        Path in which to search for CNI plugins.
        """

        self.network_name = network_config["name"]
        """
        Name of the network from the provided network config file.
        """

        self.ipam_result = None
        """
        Stores the output generated by the IPAM plugin.  This is printed
        to stdout at the end of execution.
        """

        # TODO - What config do we need here and how do we get it?
        #self.calico_config = calico_config

        self.interface = "eth0"

    def parse_cni_args(self, cni_args):
        """
        Parses the given CNI_ARGS string into key value pairs and 
        returns a dictionary containing the arguments.

        e.g "FOO=BAR;ABC=123" -> {"FOO": "BAR", "ABC": "123"}

        :param cni_args
        :return: None.
        """
        # Dictionary to return.
        args_to_return = {}

        # For each arg, split at the equal sign and add to the dictionary.
        _log.debug("Parsing CNI_ARGS: %s", cni_args)
        for arg in cni_args.split(";"):
            _log.debug("\tParsing CNI_ARG: %s", arg)
            k, v = arg.split("=")
            args_to_return[k] = v
        return args_to_return

    def execute(self):
        """Executes this plugin.
        Handles unexpected Exceptions in plugin execution.

        :return The plugin return code.
        """
        rc = 0
        try:
            self._execute()
        except SystemExit, e:
            # SystemExit indicates an error that was handled earlier
            # in the stack.  Just set the return code.
            rc = e.code 
        except BaseException:
            # An unexpected Exception has bubbled up - catch it and
            # log it out.
            _log.exception("Unhandled Exception killed plugin")
            rc = 1
        finally:
            _log.debug("Execution complete, rc=%s", rc)
            return rc

    def _execute(self):
        """Private method to execute this plugin.

        Uses the given CNI_COMMAND to determine which action to take.

        :return: None.
        """
        if self.command == CNI_CMD_ADD:
            # If an add fails, we need to clean up any changes we may
            # have made.
            try:
                self.add()
            except BaseException:
                _log.exception("Failed to add container - cleaning up")
                # TODO - Clean up after a failure.
                sys.exit(1)
        else:
            assert self.command == CNI_CMD_DELETE, \
                    "Invalid command: %s" % self.command
            self.delete()

    def add(self):
        """"Handles CNI_CMD_ADD requests. 

        Configures Calico networking and prints required json to stdout.

        :return: None.
        """
        _log.info('Configuring pod %s' % self.container_id)

        # Step 1: Assign an IP address using the given IPAM plugin.
        assigned_ip = self._assign_ip()

        # Step 2: Create the Calico endpoint object.
        endpoint = self._create_endpoint(assigned_ip)

        # Step 3: Provision the veth for this endpoint.
        endpoint = self._provision_veth(endpoint)
        
        # Step 4: Provision / set profile on the created endpoint.
        self._set_profile_on_endpoint(endpoint, self.network_name)

        # Step 5: If all successful, print the IPAM plugin's output to stdout.
        dump = json.dump(self.ipam_result)
        _log.info("Dumping info to container manager: %s", dump)
        print(dump)

        _log.info("Finished creating pod %s", self.container_id)
    
    def delete(self):
        """Handles CNI_CMD_DELETE requests.

        Remove this pod from Calico networking.

        :return: None.
        """
        _log.info('Deleting pod %s' % self.container_id)

        # Step 1: Get the Calico endpoint for this workload. If it does not
        # exist, log a warning and exit successfully.
        endpoint = self._get_endpoint()

        # Step 2: The endpoint exists - remove any IP assignments.
        self._release_ips()

        # Step 3: Delete the veth interface for this endpoint.
        netns.remove_veth(endpoint.name)

        # Step 4: Delete the Calico endpoint.
        self._remove_endpoint()

        # Step 5: Delete any profiles for this endpoint
        # TODO: This is racey and needs further thinking.
        self._remove_profile(self.network_name)

        _log.info('Finished deleting pod %s' % self.container_id)

    def _assign_ip(self):
        """Assigns and returns an IPv4 address using the IPAM plugin
        specified in the network config file.

        :return: The assigned IP address.
        """
        # TODO - Handle errors thrown by IPAM plugin
        self.ipam_result = self._call_ipam_plugin()
        _log.debug("IPAM plugin result: %s", self.ipam_result)

        try:
            # Load the response and get the assigned IP address.
            self.ipam_result = json.loads(self.ipam_result)
        except ValueError:
            _log.exception("Failed to parse IPAM response, exiting")
            sys.exit(1)

        # The request was successful.  Get the IP.
        _log.info("IPAM result: %s", self.ipam_result)
        return IPAddress(self.ipam_result["ipv4"]["ip"])

    def _release_ips(self, ip_list=[]):
        """Releases the IP address(es) for this container using the IPAM plugin
        specified in the network config file.

        :param ip_list (optional) - List of IPs to release
        :return: None.
        """
        # TODO - add ips in ip_list to network_config attribute
        # (network_config['ipam']['ips']
        # TODO - Handle errors thrown by IPAM plugin
        _log.debug("Releasing IP address")
        try:
            result = self._call_ipam_plugin()
            _log.debug("IPAM plugin result: %s", result)
        except CalledProcessError:
            _log.exception("IPAM plugin failed to un-assign IP address.")

    def _call_ipam_plugin(self):
        """Calls through to the specified IPAM plugin.
    
        Utilizes the IPAM config as specified in the CNI network
        configuration file.  A dictionary with the following form:
            {
              type: <IPAM TYPE>
            }

        :return: Response from the IPAM plugin.
        """
        # Get the plugin type and location.
        plugin_type = self.network_config['ipam']['type']
        _log.debug("IPAM plugin type: %s.  Plugin directory: %s", 
                   plugin_type, self.cni_path)
    
        # Find the correct plugin based on the given type.
        plugin_path = os.path.abspath(os.path.join(self.cni_path, plugin_type))
        _log.debug("Using IPAM plugin at: %s", plugin_path)
    
        if not os.path.isfile(plugin_path):
            _log.error("Could not find IPAM plugin: %s", plugin_path)
            sys.exit(1)
    
        # Execute the plugin and return the result.
        p = Popen(plugin_path, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = p.communicate(json.dumps(self.network_config))
        _log.debug("IPAM plugin output: \nstdout: %s\nstderr: %s", 
                   stdout, stderr)
        return stdout

    def _create_endpoint(self, assigned_ip):
        """Calls to the client to create an endpoint in the Calico datastore.

        :param assigned_ip
        :return Calico endpoint object
        """
        try:
            endpoint = self._client.create_endpoint(HOSTNAME,
                                                    ORCHESTRATOR_ID,
                                                    self.container_id,
                                                    [assigned_ip])
        except AddrFormatError:
            _log.error("Unable to create endpoint. Invalid IP address "
                       "received from IPAM plugin: %s", assigned_ip)
            # TODO - call release_ips / cleanup
            sys.exit(1)
        except KeyError:
            _log.error("Unable to create endpoint. BGP configuration not found."
                       " Is calico-node running?")
            # TODO - call release_ips / cleanup
            sys.exit(1)

        _log.info("Created Calico endpoint with IP address %s", assigned_ip)
        return endpoint

    def _remove_endpoint(self):
        """Removes the given endpoint from the Calico datastore

        :param endpoint:
        :return: None
        """
        try:
            self._client.remove_workload(hostname=HOSTNAME,
                                         orchestrator_id=ORCHESTRATOR_ID,
                                         workload_id=self.container_id)
        except KeyError:
            _log.error("Unable to remove workload with ID %s from datastore.",
                       self.container_id)
            sys.exit(1)

    def _provision_veth(self, endpoint):
        """Provisions veth for given endpoint.

        Uses the netns relative path passed in through CNI_NETNS_ENV and
        interface set in the __init__ function.

        :param endpoint
        :return Calico endpoint object
        """
        # TODO: Can we replace NETNS_ROOT with cwd?
        netns_path = '%s/%s/%s' % (NETNS_ROOT, self.container_id, self.netns)
        endpoint.mac = endpoint.provision_veth(Namespace(netns_path),
                                               self.interface)
        self._client.set_endpoint(endpoint)
        _log.info("Provisioned veth for endpoint using netns path %s on "
                  "interface %s", netns_path, self.interface)
        return endpoint

    def _set_profile_on_endpoint(self, endpoint, profile_name):
        """Sets given profile_name on given endpoint.

        If profile for given profile_name does not exist, create a profile.

        :param endpoint
        :param profile_name
        :return: None.
        """
        if not self._client.profile_exists(profile_name):
            _log.info("Creating new profile %s.", profile_name)
            self._client.create_profile(profile_name)

        _log.info("Setting profile %s on endpoint.", profile_name)
        self._client.set_profiles_on_endpoint(profile_names=[profile_name],
                                              endpoint_id=endpoint.endpoint_id)

    def _remove_profile(self, profile_name):
        """Remove the profile from datastore if it has no endpoints attached.

        :param profile_name: Name of profile to remove.
        :return: None.
        """
        profile_members = self._client.get_profile_members(profile_name)
        if self._client.profile_exists(profile_name) and len(profile_members) < 1:
            try:
                _log.info("Profile %s has no members, removing from datastore.",
                          profile_name)
                self._client.remove_profile(profile_name)
            except KeyError:
                _log.error("Cannot remove profile %s: Profile not found.",
                           profile_name)
                sys.exit(1)

    def _get_endpoint(self):
        """Finds endpoint matching given endpoint_id.

        Exits gracefully if no endpoint is found.
        Exits with an error if multiple endpoints are found.

        :param container_id:
        :return: Calico endpoint object
        """
        try:
            ep = self._client.get_endpoint(hostname=HOSTNAME,
                                           orchestrator_id=ORCHESTRATOR_ID,
                                           workload_id=self.container_id)
        except KeyError:
            _log.warning("No endpoint found matching ID %s", self.container_id)
            sys.exit(0)
        except MultipleEndpointsMatch:
            _log.error("Multiple endpoints found matching ID %s", self.container_id)
            sys.exit(1)

        return ep


def configure_logging(log_level=logging.DEBUG):
    """Configures logging for this file.

    :return None.
    """
    # If the logging directory doesn't exist, create it.
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    # Create a log handler and formtter and apply to _log.
    hdlr = logging.FileHandler(filename=LOG_PATH)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    _log.addHandler(hdlr)

    # Set the log level.
    _log.setLevel(log_level)


def main():
    """
    Main function - configures and runs the plugin.
    """
    # Get Calico config from config file.
    # TODO - Is this the correct way to get config in CNI? What config
    # do we need?

    # Configure logging.
    configure_logging()

    # Get the CNI environment. 
    env = os.environ.copy()
    _log.debug("Loaded environment:\n%s", json.dumps(env, indent=2))

    # Read the network config file from stdin. 
    config_raw = ''.join(sys.stdin.readlines()).replace('\n', '')
    network_config = json.loads(config_raw).copy()
    _log.debug("Loaded network config:\n%s", json.dumps(network_config, indent=2))

    # Create the plugin, passing in the network config, environment,
    # and the Calico configuration options.
    plugin = CniPlugin(network_config, env)

    # Call the CNI plugin.
    sys.exit(plugin.execute())


if __name__ == '__main__': # pragma: no cover
    main()
