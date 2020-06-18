#!/usr/bin/python3

import json
import waiting
import os
import pprint
import argparse
import ipaddress
import uuid
from distutils.dir_util import copy_tree
from pathlib import Path
import utils
import consts
import bm_inventory_api
import install_cluster
from logger import log
import time


# Creates ip list, if will be needed in any other place, please move to utils
def _create_ip_address_list(node_count, starting_ip_addr):
    return [str(ipaddress.ip_address(starting_ip_addr) + i) for i in range(node_count)]


# Filling tfvars json files with terraform needed variables to spawn vms
def fill_tfvars(image_path, storage_path, master_count, nodes_details):
    if not os.path.exists(consts.TFVARS_JSON_FILE):
        Path(consts.TF_FOLDER).mkdir(parents=True, exist_ok=True)
        copy_tree(consts.TF_TEMPLATE, consts.TF_FOLDER)

    with open(consts.TFVARS_JSON_FILE) as _file:
        tfvars = json.load(_file)
    network_subnet_starting_ip = str(ipaddress.ip_address(ipaddress.IPv4Network(
        nodes_details["machine_cidr"]).network_address) + 10)
    tfvars["image_path"] = image_path
    tfvars["master_count"] = min(master_count, consts.NUMBER_OF_MASTERS)
    tfvars["libvirt_master_ips"] = _create_ip_address_list(min(master_count, consts.NUMBER_OF_MASTERS),
                                                           starting_ip_addr=network_subnet_starting_ip)
    tfvars["api_vip"] = _get_vips_ips()[0]
    tfvars["libvirt_worker_ips"] = _create_ip_address_list(nodes_details["worker_count"], starting_ip_addr=str(
            ipaddress.ip_address(consts.STARTING_IP_ADDRESS) + tfvars["master_count"]))
    tfvars["libvirt_storage_pool_path"] = storage_path
    tfvars.update(nodes_details)

    with open(consts.TFVARS_JSON_FILE, "w") as _file:
        json.dump(tfvars, _file)


# Run make run terraform -> creates vms
def create_nodes(image_path, storage_path, master_count, nodes_details):
    log.info("Creating tfvars")
    fill_tfvars(image_path, storage_path, master_count, nodes_details)
    log.info("Start running terraform")
    cmd = "make run_terraform_from_skipper"
    return utils.run_command(cmd)


# Starts terraform nodes creation, waits till all nodes will get ip and will move to known status
def create_nodes_and_wait_till_registered(inventory_client, cluster, image_path, storage_path,
                                          master_count, nodes_details):
    nodes_count = master_count + nodes_details["worker_count"]
    create_nodes(image_path, storage_path=storage_path, master_count=master_count, nodes_details=nodes_details)

    # TODO: Check for only new nodes
    utils.wait_till_nodes_are_ready(nodes_count=nodes_count, network_name=nodes_details["libvirt_network_name"])
    if not inventory_client:
        log.info("No inventory url, will not wait till nodes registration")
        return

    log.info("Wait till nodes will be registered")
    waiting.wait(lambda: utils.are_all_libvirt_nodes_in_cluster_hosts(inventory_client, cluster.id,
                                                                      nodes_details["libvirt_network_name"]),
                 timeout_seconds=consts.NODES_REGISTERED_TIMEOUT,
                 sleep_seconds=10, waiting_for="Nodes to be registered in inventory service")
    log.info("Registered nodes are:")
    pprint.pprint(inventory_client.get_cluster_hosts(cluster.id))


# Set nodes roles by vm name
# If master in name -> role will be master, same for worker
def set_hosts_roles(client, cluster_id, network_name):
    added_hosts = []
    libvirt_nodes = utils.get_libvirt_nodes_mac_role_ip_and_name(network_name)
    inventory_hosts = client.get_cluster_hosts(cluster_id)

    for libvirt_mac, libvirt_metadata in libvirt_nodes.items():
        for host in inventory_hosts:
            hw = json.loads(host["hardware_info"])

            if libvirt_mac.lower() in map(lambda nic: nic["mac"].lower(), hw["nics"]):
                added_hosts.append({"id": host["id"], "role": libvirt_metadata["role"]})

    assert len(libvirt_nodes) == len(added_hosts), "All nodes should have matching inventory hosts"
    client.set_hosts_roles(cluster_id=cluster_id, hosts_with_roles=added_hosts)


def set_cluster_vips(client, cluster_id):
    cluster_info = client.cluster_get(cluster_id)
    api_vip, ingress_vip = _get_vips_ips()
    cluster_info.api_vip = api_vip
    cluster_info.ingress_vip = ingress_vip
    client.update_cluster(cluster_id, cluster_info)


def _get_vips_ips():
    network_subnet_starting_ip = str(ipaddress.ip_address(ipaddress.IPv4Network(
        args.vm_network_cidr).network_address) + 100)
    ips = _create_ip_address_list(2, starting_ip_addr=str(
        ipaddress.ip_address(network_subnet_starting_ip)))
    return ips[0], ips[1]


# TODO add config file
# Converts params from args to bm-inventory cluster params
def _cluster_create_params():
    params = {"openshift_version": args.openshift_version,
              "base_dns_domain": args.base_dns_domain,
              "cluster_network_cidr": args.cluster_network,
              "cluster_network_host_prefix":  args.host_prefix,
              "service_network_cidr": args.service_network,
              "pull_secret": args.pull_secret}
    return params


# convert params from args to terraform tfvars
def _create_node_details(cluster_name):
    return {"libvirt_worker_memory": args.worker_memory,
            "libvirt_master_memory": args.master_memory,
            "worker_count": args.number_of_workers,
            "cluster_name": cluster_name,
            "cluster_domain": args.base_dns_domain,
            "machine_cidr": args.vm_network_cidr,
            "libvirt_network_name": args.network_name,
            "libvirt_network_if": args.network_bridge}


# Create vms from downloaded iso that will connect to bm-inventory and register
# If install cluster is set , it will run install cluster command and wait till all nodes will be in installing status
def nodes_flow(client, cluster_name, cluster):
    nodes_details = _create_node_details(cluster_name)
    if cluster:
        nodes_details["cluster_inventory_id"] = cluster.id
    create_nodes_and_wait_till_registered(inventory_client=client,
                                          cluster=cluster,
                                          image_path=args.image or consts.IMAGE_PATH,
                                          storage_path=args.storage_path,
                                          master_count=args.master_count,
                                          nodes_details=nodes_details)
    if client:
        cluster_info = client.cluster_get(cluster.id)
        macs = utils.get_libvirt_nodes_macs(nodes_details["libvirt_network_name"])

        if not (cluster_info.api_vip and cluster_info.ingress_vip):
            utils.wait_till_hosts_with_macs_are_in_status(client=client, cluster_id=cluster.id, macs=macs,
                                                          statuses=[consts.NodesStatus.INSUFFICIENT])
            set_cluster_vips(client, cluster.id)
        else:
            log.info("VIPs already configured")

        set_hosts_roles(client, cluster.id, nodes_details["libvirt_network_name"])
        utils.wait_till_hosts_with_macs_are_in_status(client=client, cluster_id=cluster.id, macs=macs,
                                                      statuses=[consts.NodesStatus.KNOWN])
        log.info("Printing after setting roles")
        pprint.pprint(client.get_cluster_hosts(cluster.id))

        if args.install_cluster:
            time.sleep(10)
            install_cluster.run_install_flow(client=client, cluster_id=cluster.id,
                                             kubeconfig_path=consts.DEFAULT_CLUSTER_KUBECONFIG_PATH,
                                             pull_secret=args.pull_secret)


def main():
    client = None
    cluster = {}
    cluster_name = args.cluster_name or consts.CLUSTER_PREFIX + str(uuid.uuid4())[:8]
    # If image is passed, there is no need to create cluster and download image, need only to spawn vms with is image
    if not args.image:
        utils.recreate_folder(consts.IMAGE_FOLDER)
        client = bm_inventory_api.create_client(args.inventory_url)
        if args.cluster_id:
            cluster = client.cluster_get(cluster_id=args.cluster_id)
        else:
            cluster = client.create_cluster(cluster_name,
                                            ssh_public_key=args.ssh_key,
                                            **_cluster_create_params()
                                            )

        client.generate_and_download_image(cluster_id=cluster.id, image_path=consts.IMAGE_PATH, ssh_key=args.ssh_key,
                                           proxy_url=args.proxy_url)

    # Iso only, cluster will be up and iso downloaded but vm will not be created
    if not args.iso_only:
        nodes_flow(client, cluster_name, cluster)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run discovery flow')
    parser.add_argument('-i', '--image', help='Run terraform with given image', type=str, default="")
    parser.add_argument('-n', '--master-count', help='Masters count to spawn', type=int, default=3)
    parser.add_argument('-p', '--storage-path', help="Path to storage pool", type=str,
                        default=consts.STORAGE_PATH)
    parser.add_argument('-si', '--skip-inventory', help='Node count to spawn', action="store_true")
    parser.add_argument('-k', '--ssh-key', help="Path to ssh key", type=str,
                        default="")
    parser.add_argument('-mm', '--master-memory', help='Master memory (ram) in mb', type=int, default=8192)
    parser.add_argument('-wm', '--worker-memory', help='Worker memory (ram) in mb', type=int, default=8192)
    parser.add_argument('-nw', '--number-of-workers', help='Workers count to spawn', type=int, default=0)
    parser.add_argument('-cn', '--cluster-network', help='Cluster network with cidr', type=str, default="10.128.0.0/14")
    parser.add_argument('-hp', '--host-prefix', help='Host prefix to use', type=int, default=23)
    parser.add_argument('-sn', '--service-network', help='Network for services', type=str, default="172.30.0.0/16")
    parser.add_argument('-ps', '--pull-secret', help='Pull secret', type=str, default="")
    parser.add_argument('-ov', '--openshift-version', help='Openshift version', type=str, default="4.5")
    parser.add_argument('-bd', '--base-dns-domain', help='Base dns domain', type=str, default="redhat.com")
    parser.add_argument('-cN', '--cluster-name', help='Cluster name', type=str, default="")
    parser.add_argument('-vN', '--vm-network-cidr', help="Vm network cidr", type=str, default="192.168.126.0/24")
    parser.add_argument('-nN', '--network-name', help="Network name", type=str, default="test-infra-net")
    parser.add_argument('-in', '--install-cluster', help="Install cluster, will take latest id", action="store_true")
    parser.add_argument('-nB', '--network-bridge', help="Network bridge to use", type=str, default="tt0")
    parser.add_argument('-iO', '--iso-only', help="Create cluster and download iso, no need to spawn cluster",
                        action="store_true")
    parser.add_argument('-pU', '--proxy-url', help="Proxy url to pass to inventory cluster", type=str, default="")
    parser.add_argument('-rv', '--run-with-vips', help="Run cluster create with adding vips "
                                                       "from the same subnet as vms", type=str, default="no")
    parser.add_argument('-iU', '--inventory-url', help="Full url of remote inventory", type=str, default="")
    parser.add_argument('-id', '--cluster-id', help='Cluster id to install', type=str, default=None)

    args = parser.parse_args()
    if not args.pull_secret and args.install_cluster:
        raise Exception("Can't install cluster without pull secret, please provide one")
    main()
