package main_test

import (
	"context"
	"fmt"
	"math/rand"
	"net"
	"os"
	"os/exec"
	"strings"
	"syscall"

	"github.com/containernetworking/cni/pkg/ns"
	"github.com/containernetworking/cni/pkg/types/current"
	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
	"github.com/onsi/gomega/gexec"
	"github.com/projectcalico/cni-plugin/testutils"
	"github.com/projectcalico/cni-plugin/utils"
	api "github.com/projectcalico/libcalico-go/lib/apis/v3"
	client "github.com/projectcalico/libcalico-go/lib/clientv3"
	"github.com/projectcalico/libcalico-go/lib/names"
	"github.com/projectcalico/libcalico-go/lib/options"
	log "github.com/sirupsen/logrus"
	"github.com/vishvananda/netlink"
)

var _ = Describe("CalicoCni", func() {
	hostname, _ := os.Hostname()
	ctx := context.Background()
	calicoClient, _ := client.NewFromEnv()

	BeforeEach(func() {
		testutils.WipeEtcd()
	})

	cniVersion := os.Getenv("CNI_SPEC_VERSION")

	Describe("Run Calico CNI plugin", func() {
		Context("using host-local IPAM", func() {
			netconf := fmt.Sprintf(`
			{
			  "cniVersion": "%s",
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "log_level": "debug",
			  "datastore_type": "%s",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, cniVersion, os.Getenv("ETCD_IP"), os.Getenv("DATASTORE_TYPE"))

			It("successfully networks the namespace", func() {
				containerID, session, contVeth, contAddresses, contRoutes, contNs, err := testutils.CreateContainerWithId(netconf, "", testutils.TEST_DEFAULT_NS, "", "abc123")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result, err := testutils.GetResultForCurrent(session, cniVersion)
				if err != nil {
					log.Fatalf("Error getting result from the session: %v\n", err)
				}

				mac := contVeth.Attrs().HardwareAddr

				Expect(len(result.IPs)).Should(Equal(1))
				ip := result.IPs[0].Address.IP.String()
				result.IPs[0].Address.IP = result.IPs[0].Address.IP.To4() // Make sure the IP is respresented as 4 bytes
				Expect(result.IPs[0].Address.Mask.String()).Should(Equal("ffffffff"))

				// datastore things:
				// Profile is created with correct details
				profile, err := calicoClient.Profiles().Get(ctx, "net1", options.GetOptions{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(profile.Labels).Should(Equal(map[string]string{"net1": ""}))
				Expect(profile.Spec.Egress).Should(Equal([]api.Rule{{Action: "Allow"}}))
				Expect(profile.Spec.Ingress).Should(Equal([]api.Rule{{Action: "Allow", Source: api.EntityRule{Selector: "has(net1)"}}}))

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(ctx, options.ListOptions{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))

				ids := names.WorkloadEndpointIdentifiers{
					Node:         hostname,
					Orchestrator: "cni",
					Endpoint:     "eth0",
					Pod:          "",
					ContainerID:  containerID,
				}

				wrkload, err := ids.CalculateWorkloadEndpointName(false)
				Expect(err).NotTo(HaveOccurred())

				Expect(endpoints.Items[0].Name).Should(Equal(wrkload))
				Expect(endpoints.Items[0].Namespace).Should(Equal(testutils.TEST_DEFAULT_NS))

				Expect(endpoints.Items[0].Spec).Should(Equal(api.WorkloadEndpointSpec{
					InterfaceName: fmt.Sprintf("cali%s", containerID),
					IPNetworks:    []string{result.IPs[0].Address.String()},
					MAC:           mac.String(),
					Profiles:      []string{"net1"},
					Node:          hostname,
					Endpoint:      "eth0",
					Workload:      "",
					ContainerID:   containerID,
					Orchestrator:  "cni",
				}))

				// Routes and interface on host - there's is nothing to assert on the routes since felix adds those.
				//fmt.Println(Cmd("ip link show")) // Useful for debugging
				hostVethName := "cali" + containerID[:utils.Min(11, len(containerID))] //"cali" + containerID

				hostVeth, err := netlink.LinkByName(hostVethName)
				Expect(err).ToNot(HaveOccurred())
				Expect(hostVeth.Attrs().Flags.String()).Should(ContainSubstring("up"))
				Expect(hostVeth.Attrs().MTU).Should(Equal(1500))

				// Assert hostVeth sysctl values are set to what we expect for IPv4.
				err = testutils.CheckSysctlValue(fmt.Sprintf("/proc/sys/net/ipv4/conf/%s/proxy_arp", hostVethName), "1")
				Expect(err).ShouldNot(HaveOccurred())
				err = testutils.CheckSysctlValue(fmt.Sprintf("/proc/sys/net/ipv4/neigh/%s/proxy_delay", hostVethName), "0")
				Expect(err).ShouldNot(HaveOccurred())
				err = testutils.CheckSysctlValue(fmt.Sprintf("/proc/sys/net/ipv4/conf/%s/forwarding", hostVethName), "1")
				Expect(err).ShouldNot(HaveOccurred())

				// Assert if the host side route is programmed correctly.
				hostRoutes, err := netlink.RouteList(hostVeth, syscall.AF_INET)
				Expect(err).ShouldNot(HaveOccurred())
				Expect(hostRoutes[0]).Should(Equal(netlink.Route{
					LinkIndex: hostVeth.Attrs().Index,
					Scope:     netlink.SCOPE_LINK,
					Dst:       &result.IPs[0].Address,
					Protocol:  syscall.RTPROT_BOOT,
					Table:     syscall.RT_TABLE_MAIN,
					Type:      syscall.RTN_UNICAST,
				}))

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

				_, err = testutils.DeleteContainerWithId(netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
				Expect(err).ShouldNot(HaveOccurred())

				// Make sure there are no endpoints anymore
				endpoints, err = calicoClient.WorkloadEndpoints().List(ctx, options.ListOptions{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(0))

				// Make sure the interface has been removed from the namespace
				targetNs, _ := ns.GetNS(contNs.Path())
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

			Context("when the same hostVeth exists", func() {
				It("successfully networks the namespace", func() {
					containerID := fmt.Sprintf("con%d", rand.Uint32())
					if err := testutils.CreateHostVeth(containerID, "", "", hostname); err != nil {
						panic(err)
					}
					_, session, _, _, _, contNs, err := testutils.CreateContainerWithId(netconf, "", testutils.TEST_DEFAULT_NS, "", containerID)
					Expect(err).ShouldNot(HaveOccurred())
					Eventually(session).Should(gexec.Exit(0))

					_, err = testutils.DeleteContainerWithId(netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
					Expect(err).ShouldNot(HaveOccurred())
				})
			})
		})
	})

	Describe("Run Calico CNI plugin", func() {
		Context("deprecate Hostname for nodename", func() {
			netconf := fmt.Sprintf(`
			{
			  "cniVersion": "%s",
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "hostname": "namedHostname",
			  "datastore_type": "%s",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, cniVersion, os.Getenv("ETCD_IP"), os.Getenv("DATASTORE_TYPE"))

			It("has hostname even though deprecated", func() {
				containerID, session, _, _, _, contNs, err := testutils.CreateContainerWithId(netconf, "", testutils.TEST_DEFAULT_NS, "", "abcd1234")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result, err := testutils.GetResultForCurrent(session, cniVersion)
				if err != nil {
					log.Fatalf("Error getting result from the session: %v\n", err)
				}

				log.Printf("Unmarshalled result: %v\n", result)

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(ctx, options.ListOptions{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))

				ids := names.WorkloadEndpointIdentifiers{
					Node:         "namedHostname",
					Orchestrator: "cni",
					Endpoint:     "eth0",
					Pod:          "",
					ContainerID:  containerID,
				}

				wrkload, err := ids.CalculateWorkloadEndpointName(false)
				Expect(err).NotTo(HaveOccurred())

				Expect(endpoints.Items[0].Name).Should(Equal(wrkload))
				Expect(endpoints.Items[0].Namespace).Should(Equal(testutils.TEST_DEFAULT_NS))
				Expect(endpoints.Items[0].Spec.Node).Should(Equal("namedHostname"))

				_, err = testutils.DeleteContainerWithId(netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
				Expect(err).ShouldNot(HaveOccurred())
			})
		})
	})

	Describe("Run Calico CNI plugin", func() {
		Context("deprecate Hostname for nodename", func() {
			netconf := fmt.Sprintf(`
			{
			  "cniVersion": "%s",
			  "name": "net1",
			  "type": "calico",
			  "etcd_endpoints": "http://%s:2379",
			  "hostname": "namedHostname",
			  "nodename": "namedNodename",
			  "datastore_type": "%s",
			  "ipam": {
			    "type": "host-local",
			    "subnet": "10.0.0.0/8"
			  }
			}`, cniVersion, os.Getenv("ETCD_IP"), os.Getenv("DATASTORE_TYPE"))

			It("nodename takes precedence over hostname", func() {
				containerID, session, _, _, _, contNs, err := testutils.CreateContainerWithId(netconf, "", testutils.TEST_DEFAULT_NS, "", "abcd")
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit())

				result, err := testutils.GetResultForCurrent(session, cniVersion)
				if err != nil {
					log.Fatalf("Error getting result from the session: %v\n", err)
				}

				log.Printf("Unmarshalled result: %v\n", result)

				// The endpoint is created in etcd
				endpoints, err := calicoClient.WorkloadEndpoints().List(ctx, options.ListOptions{})
				Expect(err).ShouldNot(HaveOccurred())
				Expect(endpoints.Items).Should(HaveLen(1))

				ids := names.WorkloadEndpointIdentifiers{
					Node:         "namedNodename",
					Orchestrator: "cni",
					Endpoint:     "eth0",
					Pod:          "",
					ContainerID:  containerID,
				}

				wrkload, err := ids.CalculateWorkloadEndpointName(false)
				Expect(err).NotTo(HaveOccurred())

				Expect(endpoints.Items[0].Name).Should(Equal(wrkload))
				Expect(endpoints.Items[0].Namespace).Should(Equal(testutils.TEST_DEFAULT_NS))

				Expect(endpoints.Items[0].Spec.Node).Should(Equal("namedNodename"))

				_, err = testutils.DeleteContainerWithId(netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
				Expect(err).ShouldNot(HaveOccurred())
			})
		})
	})

	Describe("DEL", func() {
		netconf := fmt.Sprintf(`
		{
			"cniVersion": "%s",
			"name": "net1",
			"type": "calico",
			"etcd_endpoints": "http://%s:2379",
			"datastore_type": "%s",
			"ipam": {
				"type": "host-local",
				"subnet": "10.0.0.0/8"
			}
		}`, cniVersion, os.Getenv("ETCD_IP"), os.Getenv("DATASTORE_TYPE"))

		Context("when it was never called for SetUP", func() {
			Context("and a namespace does exist", func() {
				It("exits with 'success' error code", func() {
					contNs, containerID, err := testutils.CreateContainerNamespace()
					Expect(err).ShouldNot(HaveOccurred())
					exitCode, err := testutils.DeleteContainerWithId(netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
					Expect(err).ShouldNot(HaveOccurred())
					Expect(exitCode).To(Equal(0))
				})
			})

			Context("and no namespace exists", func() {
				It("exits with 'success' error code", func() {
					exitCode, err := testutils.DeleteContainer(netconf, "/not/a/real/path1234567890", "", testutils.TEST_DEFAULT_NS)
					Expect(err).ShouldNot(HaveOccurred())
					Expect(exitCode).To(Equal(0))
				})
			})
		})
	})

	Describe("with calico-ipam enabled, after creating a container", func() {
		netconf := fmt.Sprintf(`
		{
		  "cniVersion": "%s",
		  "name": "net1",
		  "type": "calico",
		  "etcd_endpoints": "http://%s:2379",
		  "datastore_type": "%s",
		  "log_level": "debug",
		  "ipam": { "type": "calico-ipam" }
		}`, cniVersion, os.Getenv("ETCD_IP"), os.Getenv("DATASTORE_TYPE"))

		var containerID string
		var workloadName string
		var endpointSpec api.WorkloadEndpointSpec
		var contNs ns.NetNS
		var result *current.Result

		checkIPAMReservation := func() {
			// IPAM reservation should still be in place.
			ipamIPs, err := calicoClient.IPAM().IPsByHandle(context.Background(), workloadName)
			ExpectWithOffset(1, err).NotTo(HaveOccurred())
			ExpectWithOffset(1, ipamIPs).To(HaveLen(1),
				"There should be an IPAM handle for endpoint")
			ExpectWithOffset(1, ipamIPs[0].String()+"/32").To(Equal(endpointSpec.IPNetworks[0]))
		}

		BeforeEach(func() {
			// Create a new ipPool.
			testutils.MustCreateNewIPPool(calicoClient, "10.0.0.0/24", false, false, true)

			var err error
			var session *gexec.Session
			log.WithField("netconf", netconf).Info("netconf")
			containerID, session, _, _, _, contNs, err = testutils.CreateContainerWithId(
				netconf, "", testutils.TEST_DEFAULT_NS, "", "badbeef")
			Expect(err).ShouldNot(HaveOccurred())
			Eventually(session).Should(gexec.Exit())

			result, err = testutils.GetResultForCurrent(session, cniVersion)
			if err != nil {
				log.Fatalf("Error getting result from the session: %v\n", err)
			}

			log.Printf("Unmarshalled result from first ADD: %v\n", result)

			// The endpoint is created in etcd
			endpoints, err := calicoClient.WorkloadEndpoints().List(ctx, options.ListOptions{Namespace: "default"})
			Expect(err).ShouldNot(HaveOccurred())
			Expect(endpoints.Items).To(HaveLen(1))

			ids := names.WorkloadEndpointIdentifiers{
				Node:         hostname,
				Orchestrator: "cni",
				Endpoint:     "eth0",
				Pod:          "",
				ContainerID:  containerID,
			}

			workloadName, err = ids.CalculateWorkloadEndpointName(false)
			Expect(err).NotTo(HaveOccurred())
			endpoint := endpoints.Items[0]
			Expect(endpoint.Name).Should(Equal(workloadName))
			endpointSpec = endpoint.Spec
			Expect(endpoint.Namespace).Should(Equal(testutils.TEST_DEFAULT_NS))
			Expect(endpoint.Spec.Node).Should(Equal(hostname))
			Expect(endpoint.Spec.Endpoint).Should(Equal("eth0"))
			Expect(endpoint.Spec.ContainerID).Should(Equal(containerID))
			Expect(endpoint.Spec.
				Orchestrator).Should(Equal("cni"))
			Expect(endpoint.Spec.Workload).Should(BeEmpty())

			// IPAM reservation should have been created.
			checkIPAMReservation()
		})

		AfterEach(func() {
			_, err := testutils.DeleteContainerWithId(
				netconf, contNs.Path(), "", testutils.TEST_DEFAULT_NS, containerID)
			Expect(err).ShouldNot(HaveOccurred())
		})

		It("a second ADD for the same container should be a no-op", func() {
			// Try to create the same container (so CNI receives the ADD for the same endpoint again)
			session, _, _, _, err := testutils.RunCNIPluginWithId(
				netconf, "", testutils.TEST_DEFAULT_NS, "", containerID, "eth0", contNs)
			Expect(err).ShouldNot(HaveOccurred())
			Eventually(session).Should(gexec.Exit(0))

			resultSecondAdd, err := testutils.GetResultForCurrent(session, cniVersion)
			if err != nil {
				log.Fatalf("Error getting result from the session: %v\n", err)
			}

			log.Printf("Unmarshalled result from second ADD: %v\n", resultSecondAdd)
			Expect(resultSecondAdd).Should(Equal(result))

			// The endpoint is created in etcd
			endpoints, err := calicoClient.WorkloadEndpoints().List(context.Background(), options.ListOptions{})
			Expect(err).ShouldNot(HaveOccurred())
			Expect(endpoints.Items).Should(HaveLen(1))
			Expect(endpoints.Items[0].Spec.Profiles).To(ConsistOf("net1"))

			// IPAM reservation should still be in place.
			checkIPAMReservation()
		})

		It("a second ADD with new profile ID should append it", func() {
			// Try to create the same container (so CNI receives the ADD for the same endpoint again)
			tweaked := strings.Replace(netconf, "net1", "net2", 1)
			session, _, _, _, err := testutils.RunCNIPluginWithId(
				tweaked, "", "", "", containerID, "", contNs)
			Expect(err).ShouldNot(HaveOccurred())
			Eventually(session).Should(gexec.Exit(0))

			resultSecondAdd, err := testutils.GetResultForCurrent(session, cniVersion)
			if err != nil {
				log.Fatalf("Error getting result from the session: %v\n", err)
			}

			log.Printf("Unmarshalled result from second ADD: %v\n", resultSecondAdd)
			Expect(resultSecondAdd).Should(Equal(result))

			// The endpoint is created in etcd
			endpoints, err := calicoClient.WorkloadEndpoints().List(context.Background(), options.ListOptions{})
			Expect(err).ShouldNot(HaveOccurred())
			Expect(endpoints.Items).Should(HaveLen(1))
			Expect(endpoints.Items[0].Spec.Profiles).To(ConsistOf("net1", "net2"))

			// IPAM reservation should still be in place.
			checkIPAMReservation()
		})

		Context("with networking rigged to fail", func() {
			BeforeEach(func() {
				// To prevent the networking atempt from succeeding, rename the old veth.
				// This leaves a route and an eth0 in place that the plugin will struggle with.
				log.Info("Breaking networking for the created interface")
				hostVeth := endpointSpec.InterfaceName
				newName := strings.Replace(hostVeth, "cali", "sali", 1)
				output, err := exec.Command("ip", "link", "set", hostVeth, "down").CombinedOutput()
				Expect(err).NotTo(HaveOccurred(), fmt.Sprintf("Output: %s", output))
				output, err = exec.Command("ip", "link", "set", hostVeth, "name", newName).CombinedOutput()
				Expect(err).NotTo(HaveOccurred(), fmt.Sprintf("Output: %s", output))
				output, err = exec.Command("ip", "link", "set", newName, "up").CombinedOutput()
				Expect(err).NotTo(HaveOccurred(), fmt.Sprintf("Output: %s", output))
			})

			It("a second ADD for the same container should leave the datastore alone", func() {
				// Try to create the same container (so CNI receives the ADD for the same endpoint again)
				log.Info("Rerunning CNI plugin")
				session, _, _, _, err := testutils.RunCNIPluginWithId(
					netconf, "", "", "", containerID, "", contNs)
				Expect(err).ShouldNot(HaveOccurred())
				Eventually(session).Should(gexec.Exit(0))

				// IPAM reservation should still be in place.
				checkIPAMReservation()
			})
		})
	})
})
