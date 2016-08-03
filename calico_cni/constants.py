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

import re
import socket

# Regex to parse CNI_ARGS.  Looks for key value pairs separated by an equals
# sign and followed either the end of the string, or a colon (indicating
# that there is another CNI_ARG key/value pair.
CNI_ARGS_RE = re.compile("([a-zA-Z0-9/\.\-\_ ]+)=([a-zA-Z0-9/\.\-\_ ]+)(?:;|$)")

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

# CNI ARGS Constants
CNI_ARGS_IP = "IP"

# Kubernetes Constants
K8S_POD_NAME = "K8S_POD_NAME"
K8S_POD_NAMESPACE = "K8S_POD_NAMESPACE"
K8S_POD_INFRA_CONTAINER_ID = "K8S_POD_INFRA_CONTAINER_ID"
K8S_CLIENT_CERTIFICATE_VAR = "k8s_client_certificate"
K8S_CLIENT_KEY_VAR = "k8s_client_key"
K8S_CERTIFICATE_AUTHORITY_VAR = "k8s_certificate_authority"

# Constants for getting Calico configuration from the network
# configuration file.
ETCD_AUTHORITY_KEY = "etcd_authority"
ETCD_ENDPOINTS_KEY = "etcd_endpoints"
LOG_LEVEL_FILE_KEY = "log_level"
LOG_LEVEL_STDERR_KEY = "log_level_stderr"
POLICY_KEY = "policy"
ASSIGN_IPV4_KEY = "assign_ipv4"
ASSIGN_IPV6_KEY = "assign_ipv6"
ARGS_KEY = "args"

# Constants for getting policy specific information
# from the policy dictionary in the network config file.
API_ROOT_KEY = "k8s_api_root"
AUTH_TOKEN_KEY = "k8s_auth_token"

# CNI Error Codes for Calico
ERR_CODE_GENERIC = 100   # Use this for all errors.

# Policy modes.
POLICY_MODE_KUBERNETES_ANNOTATIONS = "k8s-annotations"
POLICY_MODE_KUBERNETES = "k8s"

# Logging Configuration
LOG_DIR = "/var/log/calico/cni"
LOG_FORMAT = '%(asctime)s %(process)d [%(identity)s] %(levelname)s %(message)s'

# Mesos namespaced data.
MESOS_NS_KEY = "org.apache.mesos"
MESOS_NETWORK_INFO_KEY = "network_info"
MESOS_LABELS_OUTER_KEY = "labels"
MESOS_LABELS_KEY = "labels"

