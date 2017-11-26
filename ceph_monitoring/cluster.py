import re
import json
import logging
import weakref
import collections
from ipaddress import IPv4Network, IPv4Address
from typing import List, Dict, Any, Iterable, Tuple

import numpy

from cephlib.crush import load_crushmap
from cephlib.common import AttredDict
from cephlib.units import unit_conversion_coef_f
from cephlib.storage import AttredStorage


from .hw_info import get_hw_info, ssize2b, get_dev_file_name


logger = logging.getLogger("cephlib.parse")
NO_VALUE = -1


class CephOSD:
    def __init__(self):
        self.id = None
        self.reweight = None
        self.status = None
        self.host = None
        self.cluster_ip = None
        self.public_ip = None

        self.pg_count = None

        self.config = None
        self.pgs = {}

        self.journal_dev = None
        self.journal_partition = None
        self.data_dev = None
        self.data_partition = None
        self.db_dev = None
        self.db_partition = None
        self.wal_dev = None
        self.wal_partition = None

        self.version = None
        self.osd_perf = {}  # map perf field (apply_latency/journal_latency/etc) to either numpy.array with per sec
                            # avg values (-1 mean no value available), or single value for whole range
        self.historic_ops_storage_path = None

        self.used_space = None
        self.free_space = None
        self.free_perc = None
        self.procinfo = None
        self.cmdline = None
        self.storage_type = None  # filestore or bluestore

    @property
    def daemon_runs(self):
        return self.config is not None

    def __str__(self) -> str:
        return "OSD(id={0.id}, host={0.host.name}, free_perc={0.free_perc})".format(self)


class CephMonitor:
    def __init__(self):
        self.name = None
        self.status = None
        self.host = None
        self.role = None


class Pool:
    def __init__(self, pool_id: int, name: str, size: int, min_size: int, pg: int, pgp: int,
                 crush_rule: int, extra: Dict[str, Any]):
        self.id = pool_id
        self.name = name
        self.size = size
        self.min_size = min_size
        self.pg = pg
        self.pgp = pgp
        self.crush_rule = crush_rule
        self.extra = extra


class NetLoad:
    def __init__(self):
        self.send_bytes = None
        self.recv_bytes = None
        self.send_packets = None
        self.recv_packets = None
        self.send_bytes_avg = None
        self.recv_bytes_avg = None
        self.send_packets_avg = None
        self.recv_packets_avg = None


class NetAdapterAddr:
    def __init__(self, ip: IPv4Address, net: IPv4Network) -> None:
        self.ip = ip
        self.net = net


class NetworkAdapter:
    def __init__(self, name):
        self.name = name
        self.ips = []  # type: List[NetAdapterAddr]
        self.is_phy = None
        self.speed = None
        self.duplex = None
        self.load = None
        self.mtu = None


class DiskLoad:
    def __init__(self):
        self.read_bytes = None
        self.write_bytes = None
        self.read_iops = None
        self.write_iops = None
        self.io_time = None
        self.w_io_time = None
        self.iops = None
        self.queue_depth = None
        self.lat = None


class Mountable:
    def __init__(self, name: str, size: int, mountpoint: str = None, fs: str = None, free_space: int = None) -> None:
        self.name = name
        self.size = size
        self.mountpoint = mountpoint
        self.fs = fs
        self.free_space = free_space
        self.parent = None  # weakref to parent Disk or None


class Disk(Mountable):
    def __init__(self, name: str, size: int, tp: str, extra: Dict[str, Any], load: DiskLoad = None,
                 mountpoint: str = None, fs: str = None) -> None:
        Mountable.__init__(self, name, size, mountpoint=mountpoint, fs=fs)
        self.load = load
        self.tp = tp  # hdd/ssd/nvme
        self.extra = extra
        # {name: (size, mount_point)
        self.partitions = {}  # type: Dict[str, Mountable]


class Host:
    def __init__(self, name: str, stor_id):
        self.name = name
        self.stor_id = stor_id

        self.net_adapters = {}
        self.disks = {}  # type: Dict[str, Disk]
        self.storage_devs = {}  # type: Dict[str, Mountable]
        self.uptime = None
        self.perf_monitoring = None
        self.ceph_cluster_adapter = None
        self.ceph_public_adapter = None
        self.osd_ids = set()
        self.mon_name = None
        self.open_tcp_sock = None
        self.open_udp_sock = None


class TabulaRasa:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def get(self, name, default=None):
        try:
            return self.__dict__.get(name, default)
        except KeyError:
            raise AttributeError(name)

    def __contains__(self, name):
        return name in self.__dict__


netstat_fields = "recv_bytes recv_packets rerrs rdrop rfifo rframe rcompressed" + \
                 " rmulticast send_bytes send_packets serrs sdrop sfifo scolls" + \
                 " scarrier scompressed"

NetStats = collections.namedtuple("NetStats", netstat_fields)


def parse_netdev(netdev):
    info = {}
    for line in netdev.strip().split("\n")[2:]:
        adapter, data = line.split(":")
        adapter = adapter.strip()
        assert adapter not in info
        info[adapter] = NetStats(*map(int, data.split()))

    return info


def parse_meminfo(meminfo):
    info = {}
    for line in meminfo.split("\n"):
        line = line.strip()
        if line == '':
            continue
        name, data = line.split(":", 1)
        data = data.strip()
        if " " in data:
            data = data.replace(" ", "")
            assert data[-1] == 'B'
            val = ssize2b(data[:-1])
        else:
            val = int(data)
        info[name] = val
    return info


def find(lst, check, default=None):
    for obj in lst:
        if check(obj):
            return obj
    return default


def parse_txt_ceph_config(data):
    config = {}
    for line in data.strip().split("\n"):
        name, val = line.split("=", 1)
        config[name.strip()] = val.strip()
    return config


def parse_lsblkjs(data, hostname) -> Iterable[Disk]:
    for disk_js in data:
        name = disk_js['name']

        if re.match(r"sr\d+|loop\d+", name):
            continue

        if disk_js['type'] != 'disk':
            logger.warning("lsblk for node %r return device %r, which is %r not 'disk'. Ignoring it",
                           hostname, name, disk_js['type'])
            continue

        stor_tp = {
            ('sata', '1'): 'hdd',
            ('sata', '0'): 'ssd',
            ('nvme', '0'): 'nvme'
        }.get((disk_js["tran"], disk_js["rota"]))

        if stor_tp is None:
            logger.warning("Can't detect disk type for %r in node %r. tran=%r and rota=%r. Treating it as hdd",
                           name, hostname, disk_js['tran'], disk_js['rota'])
            stor_tp = 'hdd'

        dsk = Disk('/dev/' + name, disk_js['size'], tp=stor_tp, extra=disk_js,
                   mountpoint=disk_js['mountpoint'], fs=disk_js["fstype"])
        for prt_js in disk_js['children']:
            assert '/' not in prt_js['name']
            part = Mountable('/dev/' + prt_js['name'], prt_js['size'],
                             mountpoint=prt_js['mountpoint'], fs=prt_js["fstype"])
            part.parent = weakref.ref(dsk)
            dsk.partitions[part.name] = part
        yield dsk


def parse_df(data):
    lines = data.strip().split("\n")
    assert lines[0].split() == ["Filesystem", "1K-blocks", "Used", "Available", "Use%", "Mounted", "on"]
    keys = ("name", "size", None, "free", None, "mountpoint")
    for ln in lines[1:]:
        yield {key: val for key, val in zip(keys, ln.split()) if key is not None}


def parse_ipa(data) -> Dict[str, Dict[str, Any]]:
    res = {}
    for line in data.split("\n"):
        if line.strip() != '' and not line[0].isspace():
            rr = re.match(r"\d+:\s+(?P<name>[^:]*):.*?mtu\s+(?P<mtu>\d+)", line)
            if rr:
                res[rr.group('name')] = {'mtu': int(rr.group('mtu'))}
    return res


US2S = 0.000001
MS2S = 0.001


class CephCluster:
    # incorporated both ceph cluster and all nodes information
    def __init__(self, storage):
        # servers
        self.hosts = {}
        self.storage = storage
        self.report_collected_at_local = None
        self.report_collected_at_gmt = None
        self.perf_data = None
        self.ip2host = {}

        # ceph cluster
        self.osds = []
        self.mons = []
        self.pools = {}
        self.osd_map = {}  # map osd id to osd object
        self.crush = None
        self.cluster_net = None
        self.public_net = None
        self.settings = None
        self.overall_status = None
        self.health_summary = None
        self.num_pgs = None

        self.bytes_used = None
        self.bytes_total = None
        self.bytes_avail = None

        self.data_bytes = None
        self.write_bytes_sec = None
        self.op_per_sec = None
        self.pgmap_stat = None
        self.monmap_stat = None

        # synthetic props
        self.usage = None

        self.is_luminous = False
        self.has_performance_data = False

    def load(self):
        self.settings = AttredDict(**parse_txt_ceph_config(self.storage.txt.master.default_config))
        self.cluster_net = IPv4Network(self.settings['cluster_network'])
        self.public_net = IPv4Network(self.settings['public_network'])

        try:
            info = self.storage.json.master.mon_status['feature_map']['mon']['group']
            self.is_luminous = info['release'] == 'luminous'
        except KeyError:
            self.is_luminous = False

        self.crush = load_crushmap(content=self.storage.txt.master.crushmap)

        self.perf_data, self.osd_perf_dump, \
            self.osd_historic_ops_paths, self.osd_historicjs_ops_paths = self.load_perf_monitoring()

        self.has_performance_data = self.perf_data is not None

        self.load_ceph_settings()
        self.load_hosts()

        # TODO: set reweight for OSD
        self.load_PG_distribution()
        self.load_osds()
        self.load_pools()
        self.load_monitors()

        coll_time = self.storage.txt['master/collected_at'].strip()
        self.report_collected_at_local, self.report_collected_at_gmt, _ = coll_time.split("\n")
        logger.info("Cluster loaded")

    def load_perf_monitoring(self):
        if 'perf_monitoring' not in self.storage.txt:
            return None, None, None, None

        all_data = {}
        osd_perf_dump = {}
        osd_historicjs_ops_paths = {}
        osd_historic_ops_paths = {}
        osd_rr = re.compile(r"osd(\d+)$")
        for is_file, host_id in self.storage.txt.perf_monitoring:
            if is_file:
                logger.warning("Unexpected file %r in perf_monitoring folder", host_id)

            host_data = all_data[host_id] = {}
            for is_file, fname in self.storage.txt.perf_monitoring[host_id]:
                if is_file and fname == 'collected_at.csv':
                    path = "perf_monitoring/{0}/collected_at.csv".format(host_id, fname)
                    (_, _, _, units), _, data = self.storage.raw.get_array(path)
                    host_data['collected_at'] = numpy.array(data)[::2] * unit_conversion_coef_f(units, 's')
                    continue

                if is_file and fname.count('.') == 3:
                    sensor, dev, metric, ext = fname.split(".")
                    if ext == 'csv':
                        path = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        _, _, data = self.storage.raw.get_array(path)
                        host_data.setdefault(sensor, {}).setdefault(dev, {})[metric] = numpy.array(data)
                        continue
                    elif ext == 'json' and sensor == 'ceph' and metric == 'perf_dump':
                        os_id = osd_rr.match(dev)
                        assert os_id, "{0!r} don't match osdXXX name".format(dev)
                        assert os_id.group(1) not in osd_perf_dump, "Two set of perf_dump data for osd {0}"\
                            .format(os_id.group(1))
                        path = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        osd_perf_dump[int(os_id.group(1))] = json.loads(self.storage.raw.get_raw(path).decode("utf8"))
                        continue
                    elif ext == 'bin' and sensor == 'ceph' and metric == 'historic':
                        os_id = osd_rr.match(dev)
                        assert os_id, "{0!r} don't match osdXXX name".format(dev)
                        assert os_id.group(1) not in osd_perf_dump, \
                            "Two set of osd_historic_ops_paths data for osd {0}".format(os_id.group(1))
                        osd_historic_ops_paths[int(os_id.group(1))] = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        continue
                    elif ext == 'json' and sensor == 'ceph' and metric == 'historic_js':
                        os_id = osd_rr.match(dev)
                        assert os_id, "{0!r} don't match osdXXX name".format(dev)
                        assert os_id.group(1) not in osd_perf_dump, \
                            "Two set of osd_historic_ops_paths data for osd {0}".format(os_id.group(1))
                        osd_historicjs_ops_paths[int(os_id.group(1))] = "perf_monitoring/{0}/{1}".format(host_id, fname)
                        continue

                logger.warning("Unexpected %s %r in %r host performance_data folder",
                               'file' if is_file else 'folder', fname, host_id)

        return all_data, osd_perf_dump, osd_historic_ops_paths, osd_historicjs_ops_paths

    def load_ceph_settings(self):
        mstorage = self.storage.json.master
        self.overall_status = mstorage.status['health']['overall_status']

        self.health_summary = mstorage.status['health']['checks' if self.is_luminous else 'summary']
        self.num_pgs = mstorage.status['pgmap']['num_pgs']
        self.bytes_used = mstorage.status['pgmap']["bytes_used"]
        self.bytes_total = mstorage.status['pgmap']["bytes_total"]
        self.bytes_avail = mstorage.status['pgmap']["bytes_avail"]
        self.data_bytes = mstorage.status['pgmap']["data_bytes"]
        self.write_bytes_sec = mstorage.status['pgmap'].get("write_bytes_sec", 0)
        self.op_per_sec = mstorage.status['pgmap'].get("op_per_sec", 0)
        self.pgmap_stat = mstorage.status['pgmap']
        self.monmap_stat = mstorage.status['monmap']

    def load_hosts(self):
        hosts = self.storage.txt.hosts
        tcp_sock_re = re.compile('(?im)^tcp6?\\b')
        udp_sock_re = re.compile('(?im)^udp6?\\b')

        for is_file, host_ip_name in hosts:
            assert not is_file
            ip, host_name = host_ip_name.split("-", 1)

            stor_node = hosts[host_ip_name]
            jstor_node = self.storage.json.hosts[host_ip_name]

            host = self.hosts[host.name] = Host(host_name, stor_id=host_ip_name)

            lshw_xml = stor_node.get('lshw', ext='xml')

            if lshw_xml is None:
                host.hw_info = None
            else:
                try:
                    host.hw_info = get_hw_info(lshw_xml)
                except:
                    host.hw_info = None

            info = parse_meminfo(stor_node.meminfo)
            host.mem_total = info['MemTotal']
            host.mem_free = info['MemFree']
            host.swap_total = info['SwapTotal']
            host.swap_free = info['SwapFree']

            loadavg = stor_node.get('loadavg')
            host.load_5m = None if loadavg is None else float(loadavg.strip().split()[1])
            host.open_tcp_sock = len(tcp_sock_re.findall(stor_node.netstat))
            host.open_udp_sock = len(udp_sock_re.findall(stor_node.netstat))

            host.uptime = float(stor_node.uptime.split()[0])
            host.disks, host.storage_devs = self.load_hdds(host, jstor_node, stor_node)

            if self.has_performance_data:
                host.perf_monitoring = self.perf_data.get(host.stor_id)
                host.perf_monitoring['collected_at'] = host.perf_monitoring['collected_at']
            else:
                host.perf_monitoring = None

            if host.perf_monitoring:
                dtime = (host.perf_monitoring['collected_at'][-1] - host.perf_monitoring['collected_at'][0])
                self.fill_io_devices_usage_stats(host)
            else:
                dtime = None

            host.net_adapters = self.load_interfaces(host, jstor_node, stor_node, dtime)

            for adapter in host.net_adapters.values():
                for ip in adapter.ips:
                    ip_s = str(ip.ip)
                    if not ip_s.startswith('127.'):
                        if ip_s in self.ip2host:
                            logger.error("Ip %s beelong to both %s and %s. Skipping new host",
                                         ip_s, host.name, self.ip2host[ip_s].name)
                            continue

                        self.ip2host[ip_s] = host
                        if ip.ip in self.cluster_net:
                            host.ceph_cluster_adapter = adapter

                        if ip.ip in self.public_net:
                            host.ceph_public_adapter = adapter

    def load_interfaces(self, host: Host, jstor_node: AttredStorage, stor_node: AttredStorage,
                        dtime: float = None) -> Dict[str, NetworkAdapter]:
        # net_stats = parse_netdev(stor_node.netdev)
        ifs_info = parse_ipa(stor_node.ipa)
        net_adapters = {}

        for name, adapter_dct in jstor_node.interfaces.items():
            adapter_dct = adapter_dct.copy()
            dev = adapter_dct.pop('dev')
            adapter = NetworkAdapter(dev)
            adapter.__dict__.update(adapter_dct)

            try:
                adapter.mtu = ifs_info[dev]['mtu']
            except KeyError:
                logger.warning("Can't get mtu for interface %s on node %s", dev, host.name)

            net_adapters[dev] = adapter

            adapter.load = None
            if dtime and dtime > 1.0 and host.perf_monitoring:
                load = NetLoad()
                load_node = host.perf_monitoring.get('net-io', {}).get(adapter.name, {})
                for metric in 'send_bytes send_packets recv_packets recv_bytes'.split():
                    data = load_node.get(metric)
                    if data is None:
                        break
                    setattr(load, metric, data)
                    setattr(load, metric + "_avg", sum(data) / dtime)
                else:
                    adapter.load = load

        # find ip addresses
        ip_rr_s = r"\d+:\s+(?P<adapter>.*?)\s+inet\s+(?P<ip>\d+\.\d+\.\d+\.\d+)/(?P<size>\d+)"
        for line in stor_node.ipa4.split("\n"):
            match = re.match(ip_rr_s, line)
            if match is not None:
                ip_addr = IPv4Address(match.group('ip'))
                net_addr = IPv4Network("{}/{}".format(ip_addr, int(match.group('size'))), strict=False)
                dev_name = match.group('adapter')
                try:
                    adapter = net_adapters[dev_name]
                except KeyError:
                    logger.warning("Can't find adapter %r for ip %r", dev_name, ip_addr)
                    continue
                adapter.ips.append(NetAdapterAddr(ip_addr, net_addr))

        return net_adapters

    def load_hdds(self, host: Host, jstor_node: AttredStorage, stor_node: AttredStorage) -> \
                Tuple[Dict[str, Disk], Dict[str, Mountable]]:
        disks = {}  # type: Dict[str, Disk]
        storage_devs = {}  # type: Dict[str, Mountable]
        df_info = {info['name']: info for info in parse_df(stor_node.df)}
        for dsk in parse_lsblkjs(jstor_node.lsblkjs['blockdevices'], host.name):
            disks[dsk.name] = dsk
            for part in [dsk] + list(dsk.partitions.values()):
                part.free_space = df_info.get(part.name, {}).get('free', None)
            storage_devs[dsk.name] = dsk
            storage_devs.update(dsk.partitions)
        return disks, storage_devs

    def fill_io_devices_usage_stats(self, host):
        if 'block-io' not in host.perf_monitoring:
            return

        dtime = host.perf_monitoring['collected_at'][-1] - host.perf_monitoring['collected_at'][0]
        io_data = host.perf_monitoring['block-io']
        for dev, data in io_data.items():
            if dev in host.disks and dtime > 1.0:
                load = DiskLoad()
                load.read_bytes = sum(data['sectors_read']) / dtime
                load.write_bytes = sum(data['sectors_written']) / dtime
                load.read_iops = sum(data['reads_completed']) / dtime
                load.write_iops = sum(data['writes_completed']) / dtime
                load.io_time = MS2S * sum(data['io_time']) / dtime
                load.w_io_time = MS2S * sum(data['weighted_io_time']) / dtime
                load.iops = load.read_iops + load.write_iops
                load.queue_depth = load.w_io_time
                load.lat = load.w_io_time / load.iops if load.iops > 1E-5 else None
                host.disks[dev].load = load

    def load_PG_distribution(self):
        try:
            pg_dump = self.storage.json.master.pg_dump
        except AttributeError:
            pg_dump = None

        self.osd_pool_pg_2d = collections.defaultdict(lambda: collections.Counter())
        self.sum_per_pool = collections.Counter()
        self.sum_per_osd = collections.Counter()
        pool_id2name = dict((dt['poolnum'], dt['poolname'])
                            for dt in self.storage.json.master.osd_lspools)

        pg_missing_reported = False

        if pg_dump is None:
            for is_file, node in self.storage.txt.osd:
                if not is_file and node.isdigit():
                    osd_num = int(node)
                    if 'pgs' in node:
                        for pg in node.pgs.split():
                            pool_id, _ = pg.split(".")
                            pool_name = pool_id2name[int(pool_id)]
                            self.osd_pool_pg_2d[osd_num][pool_name] += 1
                            self.sum_per_pool[pool_name] += 1
                            self.sum_per_osd[osd_num] += 1
                    elif not pg_missing_reported:
                        logger.warning("Can't load PG's for osd %s - data absent." +
                                       " This might happened if you are using bluestore and you" +
                                       " cluster has too many pg. You can collect data one more time and set " +
                                       "--max-pg-dump-count XXX to value, which greater or equal to you pg count."
                                       "All subsequent messages of missing pg for osd will be ommited", osd_num)
                        pg_missing_reported = True
        else:
            for pg in pg_dump['pg_stats']:
                pool = int(pg['pgid'].split('.', 1)[0])
                for osd_num in pg['acting']:
                    pool_name = pool_id2name[pool]
                    self.osd_pool_pg_2d[osd_num][pool_name] += 1
                    self.sum_per_pool[pool_name] += 1
                    self.sum_per_osd[osd_num] += 1

    def load_osds(self):
        osd_rw_dict = dict((node['id'], node['reweight'])
                           for node in self.storage.json.master.osd_tree['nodes']
                           if node['id'] >= 0)

        osd_versions = {}
        version_rr = re.compile(r'osd.(?P<osd_id>\d+)\s*:\s*' +
                                r'\{"version":"ceph version\s+(?P<version>[^ ]*)\s+\((?P<hash>[^)]*?)\).*?"\}\s*$')

        try:
            fc = self.storage.txt.master.osd_versions
        except:
            fc = self.storage.raw.get_raw('master/osd_versions.err').decode("utf8")

        for line in fc.split("\n"):
            line = line.strip()
            if line:
                rr = version_rr.match(line.strip())
                if rr:
                    osd_versions[int(rr.group('osd_id'))] = (rr.group('version'), rr.group('hash'))

        osd_perf_scalar = {}
        for node in self.storage.json.master.osd_perf['osd_perf_infos']:
            osd_perf_scalar[node['id']] = {"apply_latency_s": node["perf_stats"]["apply_latency_ms"],
                                           "commitcycle_latency_s": node["perf_stats"]["commit_latency_ms"]}

        osd_df_map = {node['id']: node for node in self.storage.json.master.osd_df['nodes']}

        for osd_data in self.storage.json.master.osd_dump['osds']:
            osd = CephOSD()
            self.osds.append(osd)
            self.osd_map[osd.id] = osd

            osd.cluster_ip = osd_data['cluster_addr'].split(":", 1)[0]
            osd.public_ip = osd_data['public_addr'].split(":", 1)[0]
            osd.id = osd_data['osd']
            osd.reweight = osd_rw_dict[osd.id]
            osd.status = 'up' if osd_data['up'] else 'down'
            osd.version = osd_versions.get(osd.id)
            osd.pg_count = None if self.sum_per_osd is None else self.sum_per_osd[osd.id]

            try:
                osd.host = self.ip2host[osd.cluster_ip]
            except KeyError:
                logger.exception("Can't found host for osd %s, as no host own %r ip addr", osd.id, osd.cluster_ip)
                raise

            osd.host.osd_ids.add(osd.id)

            if osd.status == 'down':
                continue

            osd_stor_node = self.storage.json.osd[str(osd.id)]
            osd_disks_info = osd_stor_node.devs_cfg
            osd.storage_type = osd_disks_info['type']

            if osd.storage_type == 'filestore':
                osd.data_dev = osd_disks_info['r_data']
                osd.data_partition = osd_disks_info['data']
                osd.journal_dev = osd_disks_info['r_journal']
                osd.journal_partition = osd_disks_info['journal']
                # dstat = osd_stor_node.data.stats
                # jstat = dstat if data_dev == j_dev else osd_stor_node.journal.stats
                # osd.data_stor_stats = TabulaRasa(**dstat)
                # osd.j_stor_stats = TabulaRasa(**jstat)
                #
                # if self.has_performance_data:
                #     osd.data_stor_stats.load = osd.host.disks[get_dev_file_name(data_dev)].load
                #     osd.j_stor_stats.load = osd.host.disks[get_dev_file_name(j_dev)].load
                # else:
                #     osd.data_stor_stats.load = None
                #     osd.j_stor_stats.load = None
            else:
                assert osd.storage_type == 'bluestore'
                osd.data_dev = osd_disks_info['r_data']
                osd.data_partition = osd_disks_info['data']
                osd.db_dev = osd_disks_info['r_db']
                osd.db_partition = osd_disks_info['db']
                osd.wal_dev = osd_disks_info['r_wal']
                osd.wal_partition = osd_disks_info['wal']

            osd_df_data = osd_df_map[osd.id]
            osd.used_space = osd_df_data['kb_used'] * 1024
            osd.free_space = osd_df_data['kb_avail']  * 1024
            osd.free_perc = int((osd.free_space * 100.0) / (osd.free_space + osd.used_space) + 0.5)

            if self.has_performance_data:
                if osd.id in self.osd_perf_dump:
                    fstor = [obj["filestore"] for obj in self.osd_perf_dump[osd.id]]
                    for field in ("apply_latency", "commitcycle_latency", "journal_latency"):
                        count = numpy.array([obj[field]["avgcount"] for obj in fstor], dtype=numpy.float32)
                        values = numpy.array([obj["filestore"][field]["sum"] for obj in self.osd_perf_dump[osd.id]],
                                              dtype=numpy.float32)

                        with numpy.errstate(divide='ignore', invalid='ignore'):
                            avg_vals = (values[1:] - values[:-1]) / (count[1:] - count[:-1])

                        avg_vals[avg_vals == numpy.inf] = NO_VALUE
                        avg_vals[numpy.isnan(avg_vals)] = NO_VALUE
                        osd.osd_perf[field] = avg_vals

                    arr = numpy.array([obj['journal_wr_bytes']["avgcount"] for obj in fstor], dtype=numpy.float32)
                    osd.osd_perf["journal_ops"] = arr[1:] - arr[:-1]
                    arr = numpy.array([obj['journal_wr_bytes']["sum"] for obj in fstor], dtype=numpy.float32)
                    osd.osd_perf["journal_bytes"] = arr[1:] - arr[:-1]

                osd.osd_perf.update(osd_perf_scalar[osd.id])
                if self.osd_historic_ops_paths is not None:
                    osd.historic_ops_storage_path = self.osd_historic_ops_paths.get(osd.id)
                    osd.historicjs_ops_storage_path = self.osd_historicjs_ops_paths.get(osd.id)

            config = self.storage.txt.osd[str(osd.id)].config
            osd.config = None if config is None else AttredDict(**parse_txt_ceph_config(config))

            try:
                osd.cmdline = self.storage.raw.get_raw('osd/{0}/cmdline'.format(osd.id)).split("\x00")
            except KeyError:
                pass

            try:
                osd.procinfo = self.storage.json.osd[str(osd.id)].procinfo
            except (KeyError, AttributeError):
                pass

        self.osds.sort(key=lambda x: x.id)

    def load_pools(self):
        self.pools = {}

        for info in self.storage.json.master.osd_dump['pools']:
            pool = Pool(
                pool_id=info['pool'],
                name=info['pool_name'],
                size=info['size'],
                min_size=info['min_size'],
                pg=info['pg_num'],
                pgp=info['pg_placement_num'],
                crush_rule=info['crush_rule'] if self.is_luminous else info['crush_ruleset'],
                extra=info
            )

            self.pools[int(pool.id)] = pool

        for pool_part in self.storage.json.master.rados_df['pools']:
            if 'categories' not in pool_part:
                self.pools[int(pool_part['id'])].__dict__.update(pool_part)
            else:
                assert len(pool_part['categories']) == 1
                cat = pool_part['categories'][0].copy()
                del cat['name']
                self.pools[int(pool_part['id'])].__dict__.update(cat)

    def load_monitors(self):
        if self.is_luminous:
            mons = self.storage.json.master.status['monmap']['mons']
            mon_status = self.storage.json.master.mon_status
        else:
            srv_health = self.storage.json.master.status['health']['health']['health_services']
            assert len(srv_health) == 1
            mons = srv_health[0]['mons']

        for srv in mons:
            mon = CephMonitor()
            if self.is_luminous:
                mon.health = None
            else:
                mon.health = srv["health"]

            mon.name = srv["name"]
            mon.host = srv["name"]

            for host in self.hosts.values():
                if host.name == mon.host:
                    host.mon_name = mon.name
                    break
            else:
                msg = "Can't found host for monitor {0!r}".format(mon.name)
                logger.error(msg)
                raise ValueError(msg)

            if self.is_luminous:
                mon.kb_avail = None
                mon.avail_percent = None
            else:
                mon.kb_avail = srv["kb_avail"]
                mon.avail_percent = srv["avail_percent"]

            self.mons.append(mon)

