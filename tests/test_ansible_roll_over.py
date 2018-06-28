import datetime
import json
import logging
from time import sleep
import re
import yaml

from ceph.ceph import NodeVolume
from ceph.utils import setup_deb_repos, get_iso_file_url, setup_cdn_repos, write_docker_daemon_json, \
    search_ethernet_interface, open_firewall_port, setup_deb_cdn_repo
from ceph.utils import setup_repos, check_ceph_healthly

logger = logging.getLogger(__name__)
log = logger


def run(**kw):
    """
    Rolls updates over existing ceph-ansible deployment
    :param kw: config sample:
        config:
          ansi_config:
              ceph_test: True
              ceph_origin: distro
              ceph_stable_release: luminous
              ceph_repository: rhcs
              osd_scenario: collocated
              osd_auto_discovery: False
              journal_size: 1024
              ceph_stable: True
              ceph_stable_rh_storage: True
              public_network: 172.16.0.0/12
              fetch_directory: ~/fetch
              copy_admin_key: true
              ceph_conf_overrides:
                  global:
                    osd_pool_default_pg_num: 64
                    osd_default_pool_size: 2
                    osd_pool_default_pgp_num: 64
                    mon_max_pg_per_osd: 1024
                  mon:
                    mon_allow_pool_delete: true
              cephfs_pools:
                - name: "cephfs_data"
                  pgs: "8"
                - name: "cephfs_metadata"
                  pgs: "8"
          add:
              - node:
                  node-name: .*node15.*
                  demon:
                      - mon
    :return: non-zero on failure, zero on pass
    """
    log.info("Running test")
    ceph_nodes = kw.get('ceph_nodes')
    log.info("Running ceph ansible test")
    config = kw.get('config')
    test_data = kw.get('test_data')
    ubuntu_repo = None
    ansible_dir = '/usr/share/ceph-ansible'

    if config.get('add'):
        for added_node in config.get('add'):
            added_node = added_node.get('node')
            node_name = added_node.get('node-name')
            demon_list = added_node.get('demon')
            osds_required = [demon for demon in demon_list if demon == 'osd']
            short_name_list = [ceph_node.shortname for ceph_node in ceph_nodes]
            matcher = re.compile(node_name)
            matched_short_names = filter(matcher.match, short_name_list)
            if len(matched_short_names) > 1:
                raise RuntimeError('Multiple nodes are matching node-name {node_name}: \n{matched_short_names}'.format(
                    node_name=node_name, matched_short_names=matched_short_names))
            if len(matched_short_names) == 0:
                raise RuntimeError('No match for {node_name}'.format(node_name=node_name))
            for ceph_node in ceph_nodes:
                if ceph_node.shortname == matched_short_names[0]:
                    matched_ceph_node = ceph_node
            free_volumes = matched_ceph_node.get_free_volumes()
            if len(osds_required) > len(free_volumes):
                raise RuntimeError(
                    'Insufficient volumes on the {node_name} node. Rquired: {required} - Found: {found}'.format(
                        node_name=matched_ceph_node.shotrtname, required=len(osds_required),
                        found=len(free_volumes)))
            for osd in osds_required:
                free_volumes.pop().status = NodeVolume.ALLOCATED
            matched_ceph_node.role.update_role(demon_list)

    if config.get('ubuntu_repo'):
        ubuntu_repo = config.get('ubuntu_repo')
    if config.get('base_url'):
        base_url = config.get('base_url')
    installer_url = None
    if config.get('installer_url'):
        installer_url = config.get('installer_url')
    if config.get('skip_setup') is True:
        log.info("Skipping setup of ceph cluster")
        return 0

    # remove mgr nodes from list if build is 2.x
    build = config.get('build', '3')
    test_data['install_version'] = build
    if build.startswith('2'):
        ceph_nodes = [node for node in ceph_nodes if node.role != 'mgr']

    ceph_installer = None
    ceph_mon = None
    for ceph in ceph_nodes:
        if ceph.role == 'mon':
            open_firewall_port(ceph, port='6789', protocol='tcp')
            # for upgrades from 2.5 to 3.x, we convert mon to mgr
            # so lets open ports from 6800 to 6820
            open_firewall_port(ceph, port='6800-6820', protocol='tcp')
        if ceph.role == 'osd':
            open_firewall_port(ceph, port='6800-7300', protocol='tcp')
        if ceph.role == 'mgr':
            open_firewall_port(ceph, port='6800-6820', protocol='tcp')
        if ceph.role == 'mds':
            open_firewall_port(ceph, port='6800', protocol='tcp')
    for node in ceph_nodes:
        if node.role == 'installer':
            log.info("Setting installer node")
            ceph_installer = node
            break

    for ceph in ceph_nodes:
        if config.get('use_cdn'):
            if ceph.pkg_type == 'deb':
                if ceph.role == 'installer':
                    log.info("Enabling tools repository")
                    setup_deb_cdn_repo(ceph, config.get('build'))
            else:
                log.info("Using the cdn repo for the test")
                setup_cdn_repos(ceph_nodes, build=config.get('build'))
        else:
            if config['ansi_config'].get('ceph_repository_type') != 'iso' or \
                    config['ansi_config'].get('ceph_repository_type') == 'iso' and \
                    (ceph.role == 'installer'):
                if ceph.pkg_type == 'deb':
                    setup_deb_repos(ceph, ubuntu_repo)
                    sleep(15)
                    # install python2 on xenial
                    ceph.exec_command(sudo=True, cmd='sudo apt-get install -y python')
                    ceph.exec_command(sudo=True, cmd='apt-get install -y python-pip')
                    ceph.exec_command(sudo=True, cmd='apt-get install -y ntp')
                    ceph.exec_command(sudo=True, cmd='apt-get install -y chrony')
                    ceph.exec_command(sudo=True, cmd='pip install nose')
                else:
                    setup_repos(ceph, base_url, installer_url)
            if config['ansi_config'].get('ceph_repository_type') == 'iso' and ceph.role == 'installer':
                iso_file_url = get_iso_file_url(base_url)
                ceph.exec_command(sudo=True, cmd='mkdir -p {}/iso'.format(ansible_dir))
                ceph.exec_command(sudo=True, cmd='wget -O {}/iso/ceph.iso {}'.format(ansible_dir, iso_file_url))
        log.info("Updating metadata")
        sleep(15)
    if ceph_installer.pkg_type == 'deb':
        ceph_installer.exec_command(
            sudo=True, cmd='apt-get install -y ceph-ansible')
    else:
        ceph_installer.exec_command(
            sudo=True, cmd='yum install -y ceph-ansible')
    sleep(4)
    sleep(2)
    mon_hosts = []
    osd_hosts = []
    rgw_hosts = []
    mds_hosts = []
    mgr_hosts = []
    client_hosts = []
    num_osds = 0
    num_mons = 0
    num_mgrs = 0
    for node in ceph_nodes:
        eth_interface = search_ethernet_interface(node, ceph_nodes)
        if eth_interface is None:
            log.error('No suitable interface is found on {node}'.format(node=node.ip_address))
            return 1
        node.set_eth_interface(eth_interface)
        mon_interface = ' monitor_interface=' + node.eth_interface + ' '
        if node.role == 'mon':
            mon_host = node.shortname + ' monitor_interface=' + node.eth_interface
            mon_hosts.append(mon_host)
            num_mons += 1
        if node.role == 'mgr':
            mgr_host = node.shortname + ' monitor_interface=' + node.eth_interface
            mgr_hosts.append(mgr_host)
            num_mgrs += 1
        if node.role == 'osd':
            devices = len(node.get_allocated_volumes())
            devchar = 98
            devs = []
            for vol in range(0, devices):
                dev = '/dev/vd' + chr(devchar)
                devs.append(dev)
                devchar += 1
            reserved_devs = []
            if config['ansi_config'].get('osd_scenario') == 'non-collocated':
                reserved_devs = \
                    [raw_journal_device for raw_journal_device in set(config['ansi_config'].get('dedicated_devices'))]
            devs = [_dev for _dev in devs if _dev not in reserved_devs]
            num_osds = num_osds + len(devs)
            auto_discovey = config['ansi_config'].get('osd_auto_discovery', False)
            osd_host = node.shortname + mon_interface + \
                (" devices='" + json.dumps(devs) + "'" if not auto_discovey else '')
            osd_hosts.append(osd_host)
        if node.role == 'mds':
            mds_host = node.shortname + ' monitor_interface=' + node.eth_interface
            mds_hosts.append(mds_host)
        if node.role == 'rgw':
            rgw_host = node.shortname + ' radosgw_interface=' + node.eth_interface
            rgw_hosts.append(rgw_host)
        if node.role == 'client':
            client_host = node.shortname + ' client_interface=' + node.eth_interface
            client_hosts.append(client_host)

    hosts_file = ''
    if mon_hosts:
        mon = '[mons]\n' + '\n'.join(mon_hosts)
        hosts_file += mon + '\n'
    if mgr_hosts:
        mgr = '[mgrs]\n' + '\n'.join(mgr_hosts)
        hosts_file += mgr + '\n'
    if osd_hosts:
        osd = '[osds]\n' + '\n'.join(osd_hosts)
        hosts_file += osd + '\n'
    if mds_hosts:
        mds = '[mdss]\n' + '\n'.join(mds_hosts)
        hosts_file += mds + '\n'
    if rgw_hosts:
        rgw = '[rgws]\n' + '\n'.join(rgw_hosts)
        hosts_file += rgw + '\n'
    if client_hosts:
        client = '[clients]\n' + '\n'.join(client_hosts)
        hosts_file += client + '\n'

    log.info('Generated hosts file: \n{file}'.format(file=hosts_file))
    host_file = ceph_installer.write_file(
        sudo=True, file_name='{}/hosts'.format(ansible_dir), file_mode='w')
    host_file.write(hosts_file)
    host_file.flush()
    if config.get('ansi_config').get('containerized_deployment') and config.get('docker-insecure-registry') and \
            config.get('ansi_config').get('ceph_docker_registry'):
        insecure_registry = '{{"insecure-registries" : ["{registry}"]}}'.format(
            registry=config.get('ansi_config').get('ceph_docker_registry'))
        log.warn('Adding insecure registry:\n{registry}'.format(registry=insecure_registry))
        for node in ceph_nodes:
            write_docker_daemon_json(insecure_registry, node)

    # use the provided sample file as main site.yml
    if config.get('ansi_config').get('containerized_deployment') is True:
        ceph_installer.exec_command(
            sudo=True,
            cmd='cp -R {ansible_dir}/site-docker.yml.sample {ansible_dir}/site.yml'.format(ansible_dir=ansible_dir))
    else:
        ceph_installer.exec_command(
            sudo=True, cmd='cp -R {ansible_dir}/site.yml.sample {ansible_dir}/site.yml'.format(ansible_dir=ansible_dir))

    if config.get('ansi_config').get('fetch_directory') is None:
        # default fetch directory is not writeable, lets use local one if not set
        config['ansi_config']['fetch_directory'] = '~/fetch/'
    gvar = yaml.dump(config.get('ansi_config'), default_flow_style=False)
    log.info("global vars " + gvar)
    gvars_file = ceph_installer.write_file(
        sudo=True, file_name='{}/group_vars/all.yml'.format(ansible_dir), file_mode='w')
    gvars_file.write(gvar)
    gvars_file.flush()

    if ceph_installer.pkg_type == 'rpm':
        out, rc = ceph_installer.exec_command(cmd='rpm -qa | grep ceph')
    else:
        out, rc = ceph_installer.exec_command(sudo=True, cmd='apt-cache search ceph')
    log.info("Ceph versions " + out.read())
    out, rc = ceph_installer.exec_command(
        cmd='cd {} ; ANSIBLE_STDOUT_CALLBACK=debug; ansible-playbook -vv -i hosts site.yml'.format(ansible_dir),
        long_running=True)

    if rc != 0:
        log.error("Failed during deployment")
        return rc

    # Add all clients
    for node in ceph_nodes:
        if node.role == 'mon':
            ceph_mon = node
            break
    mon_container = None
    if config.get('ansi_config').get('containerized_deployment') is True:
        mon_container = 'ceph-mon-{host}'.format(host=ceph_mon.hostname)
    # check if all osd's are up and in
    timeout = 300
    if config.get('timeout'):
        timeout = datetime.timedelta(seconds=config.get('timeout'))
    # add test_data for later use by upgrade test etc
    test_data['ceph-ansible'] = {'num-osds': num_osds, 'num-mons': num_mons, 'rhbuild': build}

    # create rbd pool used by tests/workunits
    if not build.startswith('2'):
        if config.get('ansi_config').get('containerized_deployment') is True:
            ceph_mon.exec_command(
                sudo=True, cmd='docker exec {container} ceph osd pool create rbd 64 64'.format(container=mon_container))
            ceph_mon.exec_command(
                sudo=True, cmd='docker exec {container} ceph osd pool application enable rbd rbd --yes-i-really-mean-it'
                    .format(container=mon_container))
        else:
            ceph_mon.exec_command(sudo=True, cmd='ceph osd pool create rbd 64 64')
            ceph_mon.exec_command(sudo=True, cmd='ceph osd pool application enable rbd rbd --yes-i-really-mean-it')
    if check_ceph_healthly(ceph_mon, num_osds, num_mons, mon_container, timeout) != 0:
        return 1
    return rc