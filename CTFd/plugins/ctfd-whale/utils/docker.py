import json
import random
import uuid
from collections import OrderedDict

import docker
from flask import current_app

from CTFd.utils import get_config

from .cache import CacheProvider
from .exceptions import WhaleError


class DockerUtils:
    @staticmethod
    def init():
        try:
            DockerUtils.client = docker.DockerClient(base_url=get_config("whale:docker_api_url"))
            # docker-py is thread safe: https://github.com/docker/docker-py/issues/619
        except Exception:
            raise WhaleError(
                'Docker 连接错误\n'
                '请确保docker api url(第一个配置项)是正确的\n'
                '如果你使用的是unix:///var/run/docker.sock，检查socket是否被正确映射'
            )
        credentials = get_config("whale:docker_credentials")
        if credentials and credentials.count(':') == 1:
            try:
                DockerUtils.client.login(*credentials.split(':'))
            except Exception:
                raise WhaleError('docker.io 登录失败，请检查您的凭证')

    @staticmethod
    def add_container(container):
        if container.challenge.docker_image.startswith("{"):
            DockerUtils._create_grouped_container(DockerUtils.client, container)
        else:
            DockerUtils._create_standalone_container(DockerUtils.client, container)

    @staticmethod
    def _create_standalone_container(client, container):
        dns = get_config("whale:docker_dns", "").split(",")
        node = DockerUtils.choose_node(
            container.challenge.docker_image,
            get_config("whale:docker_swarm_nodes", "").split(",")
        )

        client.services.create(
            image=container.challenge.docker_image,
            name=f'{container.user_id}-{container.uuid}',
            env={'FLAG': container.flag}, dns_config=docker.types.DNSConfig(nameservers=dns),
            networks=[get_config("whale:docker_auto_connect_network", "ctfd_frp-containers")],
            resources=docker.types.Resources(
                mem_limit=DockerUtils.convert_readable_text(
                    container.challenge.memory_limit),
                cpu_limit=int(container.challenge.cpu_limit * 1e9)
            ),
            labels={
                'whale_id': f'{container.user_id}-{container.uuid}'
            },  # for container deletion
            constraints=['node.labels.name==' + node],
            endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={})
        )

    @staticmethod
    def _create_grouped_container(client, container):
        range_prefix = CacheProvider(app=current_app).get_available_network_range()

        ipam_pool = docker.types.IPAMPool(subnet=range_prefix)
        ipam_config = docker.types.IPAMConfig(
            driver='default', pool_configs=[ipam_pool])
        network_name = f'{container.user_id}-{container.uuid}'
        network = client.networks.create(
            network_name, internal=True,
            ipam=ipam_config, attachable=True,
            labels={'prefix': range_prefix},
            driver="overlay", scope="swarm"
        )

        dns = []
        containers = get_config("whale:docker_auto_connect_containers", "").split(",")
        for c in containers:
            if not c:
                continue
            network.connect(c)
            if "dns" in c:
                network.reload()
                for name in network.attrs['Containers']:
                    if network.attrs['Containers'][name]['Name'] == c:
                        dns.append(network.attrs['Containers'][name]['IPv4Address'].split('/')[0])

        has_processed_main = False
        try:
            images = json.loads(
                container.challenge.docker_image,
                object_pairs_hook=OrderedDict
            )
        except json.JSONDecodeError:
            raise WhaleError(
                "镜像解析错误\n"
                "请检查拉取镜像字符串"
            )
        for name, image in images.items():
            if has_processed_main:
                container_name = f'{container.user_id}-{container.uuid}'
            else:
                container_name = f'{container.user_id}-{uuid.uuid4()}'
                node = DockerUtils.choose_node(image, get_config("whale:docker_swarm_nodes", "").split(","))
                has_processed_main = True
            client.services.create(
                image=image, name=container_name, networks=[
                    docker.types.NetworkAttachmentConfig(network_name, aliases=[name])
                ],
                env={'FLAG': container.flag},
                dns_config=docker.types.DNSConfig(nameservers=dns),
                resources=docker.types.Resources(
                    mem_limit=DockerUtils.convert_readable_text(
                        container.challenge.memory_limit
                    ),
                    cpu_limit=int(container.challenge.cpu_limit * 1e9)),
                labels={
                    'whale_id': f'{container.user_id}-{container.uuid}'
                },  # for container deletion
                hostname=name, constraints=['node.labels.name==' + node],
                endpoint_spec=docker.types.EndpointSpec(mode='dnsrr', ports={})
            )

    @staticmethod
    def remove_container(container):
        whale_id = f'{container.user_id}-{container.uuid}'

        for s in DockerUtils.client.services.list(filters={'label': f'whale_id={whale_id}'}):
            s.remove()

        networks = DockerUtils.client.networks.list(names=[whale_id])
        if len(networks) > 0:  # is grouped containers
            auto_containers = get_config("whale:docker_auto_connect_containers", "").split(",")
            redis_util = CacheProvider(app=current_app)
            for network in networks:
                for container in auto_containers:
                    try:
                        network.disconnect(container, force=True)
                    except Exception:
                        pass
                redis_util.add_available_network_range(network.attrs['Labels']['prefix'])
                network.remove()

    @staticmethod
    def convert_readable_text(text):
        lower_text = text.lower()

        if lower_text.endswith("k"):
            return int(text[:-1]) * 1024

        if lower_text.endswith("m"):
            return int(text[:-1]) * 1024 * 1024

        if lower_text.endswith("g"):
            return int(text[:-1]) * 1024 * 1024 * 1024

        return 0

    @staticmethod
    def choose_node(image, nodes):
        win_nodes = []
        linux_nodes = []
        for node in nodes:
            if node.startswith("windows"):
                win_nodes.append(node)
            else:
                linux_nodes.append(node)
        try:
            tag = image.split(":")[1:]
            if len(tag) and tag[0].startswith("windows"):
                return random.choice(win_nodes)
            return random.choice(linux_nodes)
        except IndexError:
            raise WhaleError(
                '没有合适的节点.\n'
                '如果你是第一次使用Whale, \n'
                '请正确设置集群节点并将其标记为\n'
                'docker node update --label-add "name=linux-1" $(docker node ls -q)'
            )
