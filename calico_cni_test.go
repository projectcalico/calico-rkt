package main_test

import (
	"encoding/json"
	"fmt"
	"os"

	"net"

	"syscall"

	"github.com/containernetworking/cni/pkg/ns"
	"github.com/containernetworking/cni/pkg/types"
	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
	"github.com/onsi/gomega/gexec"
	. "github.com/projectcalico/cni-plugin/test_utils"
	"github.com/projectcalico/libcalico-go/lib/api"
	"github.com/projectcalico/libcalico-go/lib/client"
	cnet "github.com/projectcalico/libcalico-go/lib/net"
	"github.com/projectcalico/libcalico-go/lib/testutils"
	"github.com/vishvananda/netlink"
)

// Some ideas for more tests
// Test that both etcd_endpoints and etcd_authity can be used
// Test k8s
// test bad network name
// badly formatted netconf
// vary the MTU
// Existing endpoint

var calicoClient *client.Client

func init() {
	var err error
	calicoClient, err = testutils.NewClient("")
	if err != nil {
		panic(err)
	}
}

var _ = Describe("CalicoCni", func() {
	hostname, _ := os.Hostname()
	BeforeEach(func() {
		WipeEtcd()
	})

	Describe("Run Calico CNI plugin", func() {
		Context("using host-local IPAM", func() {
			netconf := fmt.Sprintf(`
			{
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, os.Getenv("ETCD_IP"))

			It("successfully networks the namespace", func() {
				containerID, netnspath, session, contVeth, contAddresses, contRoutes, err := CreateContainer(netconf, "", "")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result := types.Result{}
				if err := json.Unmarshal(session.Out.Contents(), &result); err != nil {
					panic(err)
				}
				mac := contVeth.Attrs().HardwareAddr

				ip := result.IP4.IP.IP.String()
				result.IP4.IP.IP = result.IP4.IP.IP.To4() // Make sure the IP is respresented as 4 bytes
				Expect(result.IP4.IP.Mask.String()).Should(Equal("ffffffff"))

				// datastore things:
				// Profile is created with correct details
				profile, err := calicoClient.Profiles().Get(api.ProfileMetadata{Name: "net1"})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(profile.Metadata.Labels).Should(HaveKeyWithValue("projectcalico.org/network", "net1"))
				Expect(profile.Spec.EgressRules).Should(Equal([]api.Rule{{Action: "allow"}}))
				Expect(profile.Spec.IngressRules).Should(Equal([]api.Rule{{Action: "allow", Source: api.EntityRule{Selector: "projectcalico.org/network == \"net1\""}}}))

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(api.WorkloadEndpointMetadata{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))
				Expect(endpoints.Items[0].Metadata).Should(Equal(api.WorkloadEndpointMetadata{
					Node:         hostname,
					Name:         "eth0",
					Workload:     containerID,
					Orchestrator: "cni",
				}))

				Expect(endpoints.Items[0].Spec).Should(Equal(api.WorkloadEndpointSpec{
					InterfaceName: fmt.Sprintf("cali%s", containerID),
					IPNetworks:    []cnet.IPNet{{result.IP4.IP}},
					MAC:           &cnet.MAC{HardwareAddr: mac},
					Profiles:      []string{"net1"},
				}))

				// Routes and interface on host - there's is nothing to assert on the routes since felix adds those.
				//fmt.Println(Cmd("ip link show")) // Useful for debugging
				hostVeth, err := netlink.LinkByName("cali" + containerID)
				Expect(err).ToNot(HaveOccurred())
				Expect(hostVeth.Attrs().Flags.String()).Should(ContainSubstring("up"))
				Expect(hostVeth.Attrs().MTU).Should(Equal(1500))

				// Routes and interface in netns
				Expect(contVeth.Attrs().Flags.String()).Should(ContainSubstring("up"))

				// Assume the first IP is the IPv4 address
				Expect(contAddresses[0].IP.String()).Should(Equal(ip))
				Expect(contRoutes).Should(SatisfyAll(ContainElement(netlink.Route{
					LinkIndex: contVeth.Attrs().Index,
					Gw:        net.IPv4(169, 254, 1, 1).To4(),
					Protocol:  syscall.RTPROT_BOOT,
					Table:     syscall.RT_TABLE_MAIN,
					Type:      syscall.RTN_UNICAST,
				}),
					ContainElement(netlink.Route{
						LinkIndex: contVeth.Attrs().Index,
						Scope:     netlink.SCOPE_LINK,
						Dst:       &net.IPNet{IP: net.IPv4(169, 254, 1, 1).To4(), Mask: net.CIDRMask(32, 32)},
						Protocol:  syscall.RTPROT_BOOT,
						Table:     syscall.RT_TABLE_MAIN,
						Type:      syscall.RTN_UNICAST,
					})))

				_, err = DeleteContainer(netconf, netnspath, "")
				Expect(err).ShouldNot(HaveOccurred())

				// Make sure there are no endpoints anymore
				endpoints, err = calicoClient.WorkloadEndpoints().List(api.WorkloadEndpointMetadata{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(0))

				// Make sure the interface has been removed from the namespace
				targetNs, _ := ns.GetNS(netnspath)
				err = targetNs.Do(func(_ ns.NetNS) error {
					_, err = netlink.LinkByName("eth0")
					return err
				})
				Expect(err).Should(HaveOccurred())
				Expect(err.Error()).Should(Equal("Link not found"))

				// Make sure the interface has been removed from the host
				_, err = netlink.LinkByName("cali" + containerID)
				Expect(err).Should(HaveOccurred())
				Expect(err.Error()).Should(Equal("Link not found"))

			})
		})
	})

	Describe("Run Calico CNI plugin", func() {
		Context("depricate Hostname for nodename", func() {
			netconf := fmt.Sprintf(`
			{
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "hostname": "namedHostname",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, os.Getenv("ETCD_IP"))

			It("has hostname even though deprecated", func() {
				containerID, netnspath, session, _, _, _, err := CreateContainer(netconf, "", "")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result := types.Result{}
				if err := json.Unmarshal(session.Out.Contents(), &result); err != nil {
					panic(err)
				}

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(api.WorkloadEndpointMetadata{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))
				Expect(endpoints.Items[0].Metadata).Should(Equal(api.WorkloadEndpointMetadata{
					Node:         "namedHostname",
					Name:         "eth0",
					Workload:     containerID,
					Orchestrator: "cni",
				}))

				_, err = DeleteContainer(netconf, netnspath, "")
				Expect(err).ShouldNot(HaveOccurred())
			})
		})
	})

	Describe("Run Calico CNI plugin", func() {
		Context("depricate Hostname for nodename", func() {
			netconf := fmt.Sprintf(`
			{
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "hostname": "namedHostname",
			  "nodename": "namedNodename",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, os.Getenv("ETCD_IP"))

			It("nodename takes precedence over hostname", func() {
				containerID, netnspath, session, _, _, _, err := CreateContainer(netconf, "", "")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result := types.Result{}
				if err := json.Unmarshal(session.Out.Contents(), &result); err != nil {
					panic(err)
				}

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(api.WorkloadEndpointMetadata{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))
				Expect(endpoints.Items[0].Metadata).Should(Equal(api.WorkloadEndpointMetadata{
					Node:         "namedNodename",
					Name:         "eth0",
					Workload:     containerID,
					Orchestrator: "cni",
				}))

				_, err = DeleteContainer(netconf, netnspath, "")
				Expect(err).ShouldNot(HaveOccurred())
			})
		})
	})

	Describe("DEL", func() {
		netconf := fmt.Sprintf(`
		{
			"name": "net1",
			"type": "calico",
			"etcd_endpoints": "http://%s:2379",
			"ipam": {
				"type": "host-local",
				"subnet": "10.0.0.0/8"
			}
		}`, os.Getenv("ETCD_IP"))

		Context("when it was never called for SetUP", func() {
			Context("and a namespace does exist", func() {
				It("exits with 'success' error code", func() {
					_, _, netnspath, err := CreateContainerNamespace()
					Expect(err).ShouldNot(HaveOccurred())
					exitCode, err := DeleteContainer(netconf, netnspath, "")
					Expect(err).ShouldNot(HaveOccurred())
					Expect(exitCode).To(Equal(0))
				})
			})

			Context("and no namespace exists", func() {
				It("exits with 'success' error code", func() {
					exitCode, err := DeleteContainer(netconf, "/not/a/real/path1234567890", "")
					Expect(err).ShouldNot(HaveOccurred())
					Expect(exitCode).To(Equal(0))
				})
			})
		})
	})
})
