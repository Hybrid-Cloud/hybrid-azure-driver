#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr
import re
import six
import time
from nova.compute import arch
from nova.compute import hv_type
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
from nova import conf
from nova import exception as nova_ex
from nova import image
from nova.i18n import _LW, _LE, _LI
from nova.virt.azureapi.adapter import Azure
from nova.virt.azureapi import constant
from nova.virt.azureapi import exception
from nova.virt import driver
from nova.virt.hardware import InstanceInfo
from nova.volume import cinder
from oslo_log import log as logging
from oslo_service import loopingcall

CONF = conf.CONF
LOG = logging.getLogger(__name__)
VOLUME_CONTAINER = 'volumes'
SNAPSHOT_CONTAINER = 'snapshots'
VHDS_CONTAINER = 'vhds'
IMAGE_CONTAINER = 'images'
AZURE = 'azure'
USER_NAME = 'azureuser'
VHD_EXT = 'vhd'
SNAPSHOT_PREFIX = 'snapshot'
VOLUME_PREFIX = 'volume'
INSTANCE_PREFIX = 'instance'
IMAGE_PREFIX = 'image'

# TODO(haifeng) need complete according to image mapping.
LINUX_OFFER = ['UbuntuServer', 'RedhatServer']
WINDOWS_OFFER = ['WindowsServerEssentials']

LINUX_OS = 'linux'
WINDOWS_OS = 'windows'


class AzureDriver(driver.ComputeDriver):
    capabilities = {
        "has_imagecache": False,
        "supports_recreate": True,
        "supports_migrate_to_same_host": True,
        "supports_attach_interface": False,
        "supports_device_tagging": False
    }

    def __init__(self, virtapi):
        super(AzureDriver, self).__init__(virtapi)
        try:
            self.azure = Azure()
            self.disks = self.azure.compute.disks
            self.images = self.azure.compute.images
        except Exception as e:
            msg = (_LI("Initialize Azure Adapter failed. reason: %"),
                   six.text_type(e))
            LOG.error(msg)
            raise nova_ex.NovaException(message=msg)
        self.compute = self.azure.compute
        self.network = self.azure.network
        # self.storage = self.azure.storage
        self.resource = self.azure.resource
        # self.blob = self.azure.blob

        self._volume_api = cinder.API()
        self._image_api = image.API()

        self.cleanup_time = time.time()
        self.residual_nics = []

    # def _get_blob_name(self, name):
    #     """Get blob name from volume name"""
    #     return '{}.{}'.format(name, VHD_EXT)

    def _is_valid_cidr(self, address):
        """Verify that address represents a valid CIDR address.

        :param address: Value to verify
        :type address: string
        :returns: bool

        .. versionadded:: 3.8
        """
        try:
            # Validate the correct CIDR Address
            netaddr.IPNetwork(address)
        except (TypeError, netaddr.AddrFormatError):
            return False

        # Prior validation partially verify /xx part
        # Verify it here
        ip_segment = address.split('/')

        if (len(ip_segment) <= 1 or
                    ip_segment[1] == ''):
            return False

        return True

    def _precreate_network(self):
        """Pre Create Network info in Azure."""
        # check cidr format
        net_cidr = CONF.azure.vnet_cidr
        subnet_cidr = CONF.azure.vsubnet_cidr
        if not (self._is_valid_cidr(net_cidr) and
                    self._is_valid_cidr(subnet_cidr)):
            msg = 'Invalid network: %(net_cidr)s/subnet: %(subnet_cidr)s' \
                  ' CIDR' % dict(net_cidr=net_cidr, subnet_cidr=subnet_cidr)
            LOG.error(msg)
            raise exception.NetworkCreateFailure(reason=msg)
        # Creaet Network
        try:
            nets = self.network.virtual_networks.list(
                CONF.azure.resource_group)
            net_exist = False
            for i in nets:
                if i.name == CONF.azure.vnet_name:
                    net_exist = True
                    break
            if not net_exist:
                network_info = dict(location=CONF.azure.location,
                                    address_space=dict(
                                        address_prefixes=[net_cidr]))
                async_vnet_creation = \
                    self.network.virtual_networks.create_or_update(
                        CONF.azure.resource_group,
                        CONF.azure.vnet_name,
                        network_info)
                async_vnet_creation.wait(CONF.azure.async_timeout)
                LOG.info(_LI("Create Network"))
        except Exception as e:
            msg = six.text_type(e)
            ex = exception.NetworkCreateFailure(reason=msg)
            LOG.exception(msg)
            raise ex

        # Create Subnet
        try:
            # subnet can't recreate, check existing before create.
            subnets = self.network.subnets.list(
                CONF.azure.resource_group,
                CONF.azure.vnet_name)
            subnet_exist = False
            subnet_details = None
            for i in subnets:
                if i.name == CONF.azure.vsubnet_name:
                    subnet_exist = True
                    subnet_details = i
                    break
            if not subnet_exist:
                subnet_info = {'address_prefix': subnet_cidr}
                async_subnet_creation = self.network.subnets.create_or_update(
                    CONF.azure.resource_group,
                    CONF.azure.vnet_name,
                    CONF.azure.vsubnet_name,
                    subnet_info
                )
                subnet_details = async_subnet_creation.result()
        except Exception as e:
            # delete network if subnet create fail.
            try:
                async_vm_action = self.network.virtual_networks.delete(
                    CONF.azure.resource_group, CONF.azure.vnet_name)
                async_vm_action.wait(CONF.azure.async_timeout)
                LOG.info(_LI("Deleted Network %s after Subnet create "
                             "failed."), CONF.azure.vnet_name)
            except Exception:
                LOG.error(_LE('Delete Network %s failed after Subnet create '
                              'failed.'), CONF.azure.vnet_name)
            msg = six.text_type(e)
            ex = exception.SubnetCreateFailure(reason=msg)
            LOG.exception(msg)
            raise ex
        CONF.set_override('vsubnet_id', subnet_details.id, 'azure')
        LOG.info(_LI("Create/Update Subnet: %s"), CONF.azure.vsubnet_id)

    def init_host(self, host):
        """All resources initial for driver can be repeate create, so no check

        exist needed, and no roll back needed, as anyway we need to create
        these resources.
        """
        self._precreate_network()
        LOG.info(_LI("Create/Update Ntwork and Subnet, Done."))

    def get_host_ip_addr(self):
        return CONF.my_ip

    def get_available_nodes(self, refresh=False):
        return ['azure-{}'.format(CONF.azure.location)]

    def list_instances(self):
        """Return the names of all the instances known to the virtualization

        layer, as a list.
        """
        instances = []
        try:
            pages = self.compute.virtual_machines.list(
                CONF.azure.resource_group)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.InstanceListFailure(reason=six.text_type(e))
            raise ex
        else:
            if pages:
                for i in pages:
                    instances.append(i.name)
        return instances

    def list_instance_uuids(self):
        """Return the UUIDS of all the instances known to the virtualization

        layer, as a list. azure vm.name is vm.uuid in openstack.
        """
        return self.list_instances()

    def get_info(self, instance):
        """Get the current status of an instance

        state for azure:running, deallocating, deallocated,
        stopping , stopped
        :raise: nova_ex.
        """
        shutdown_staues = ['deallocating', 'deallocated',
                           'stopping', 'stopped']
        instance_id = instance.uuid
        state = power_state.NOSTATE
        status = 'Unkown'
        try:
            vm = self.compute.virtual_machines.get(
                CONF.azure.resource_group, instance_id, expand='instanceView')
        # azure may raise msrestazure.azure_exceptions CloudError
        except exception.CloudError as e:
            msg = six.text_type(e)
            if 'ResourceNotFound' in msg:
                raise nova_ex.InstanceNotFound(instance_id=instance.uuid)
            else:
                LOG.exception(msg)
                ex = exception.InstanceGetFailure(reason=six.text_type(e),
                                                  instance_uuid=instance_id)
                raise ex
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.InstanceGetFailure(reason=six.text_type(e),
                                              instance_uuid=instance_id)
            raise ex
        else:
            LOG.debug('vm info is: {}'.format(vm))
            if vm and hasattr(vm, 'instance_view') and \
                    hasattr(vm.instance_view, 'statuses') and \
                            vm.instance_view.statuses is not None:
                for i in vm.instance_view.statuses:
                    if hasattr(i, 'code') and \
                            i.code and 'PowerState' in i.code:
                        status = i.code.split('/')[-1]
                        if 'running' == status:
                            state = power_state.RUNNING
                        elif status in shutdown_staues:
                            state = power_state.SHUTDOWN
                        break
            LOG.info(_LI('vm: %(instance_id)s state is : %(status)s'),
                     dict(instance_id=instance_id, status=status))
        return InstanceInfo(state=state, id=instance_id)

    def get_available_resource(self, nodename):
        """get available resource and delete residual resources."""
        curent_time = time.time()
        if curent_time - self.cleanup_time > CONF.azure.cleanup_span:
            self.cleanup_time = curent_time
            self._cleanup_deleted_os_disks()
            self._cleanup_deleted_nics()
        usage_family = 'basicAFamily'
        try:
            page = self.compute.usage.list(CONF.azure.location)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.ComputeUsageListFailure(reason=six.text_type(e))
            raise ex
        usages = [i for i in page]
        cores = 0
        cores_used = 0
        for i in usages:
            if hasattr(i, 'name') and hasattr(i.name, 'value'):
                if usage_family == i.name.value:
                    cores = i.limit if hasattr(i, 'limit') else 0
                    cores_used = i.current_value \
                        if hasattr(i, 'current_value') else 0
                    break
        return {'vcpus': cores,
                'memory_mb': 100000000,
                'local_gb': 100000000,
                'vcpus_used': cores_used,
                'memory_mb_used': 0,
                'local_gb_used': 0,
                'hypervisor_type': hv_type.HYPERV,
                'hypervisor_version': 300,
                'hypervisor_hostname': nodename,
                'cpu_info': '{"model": ["Intel(R) Xeon(R) CPU E5-2670 0 @ '
                            '2.60GHz"], "topology": {"cores": 16, "threads": '
                            '32}}',
                'supported_instances': [(arch.I686, hv_type.HYPERV,
                                         vm_mode.HVM),
                                        (arch.X86_64, hv_type.HYPERV,
                                         vm_mode.HVM)],
                'numa_topology': None
                }

    def _prepare_network_profile(self, instance_uuid):
        """Create a Network Interface for a VM."""
        network_interface = {
            'location': CONF.azure.location,
            'ip_configurations': [{
                'name': instance_uuid,
                'subnet': {
                    'id': CONF.azure.vsubnet_id
                }
            }]
        }
        try:
            async_nic_creation = \
                self.network.network_interfaces.create_or_update(
                    CONF.azure.resource_group,
                    instance_uuid,
                    network_interface)
            nic = async_nic_creation.result()
            LOG.info(_LI("Create a Nic: %s"), nic.id)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.NetworkInterfaceCreateFailure(
                reason=six.text_type(e), instance_uuid=instance_uuid)
            raise ex
        network_profile = {
            'network_interfaces': [{
                'id': nic.id
            }]
        }
        return network_profile

    def _get_name_from_id(self, prefix, resource_id):
        return '{}-{}'.format(prefix, resource_id)

    def _get_image_from_mapping(self, image_meta):
        image_name = image_meta.name
        image_ref = constant.IMAGE_MAPPING.get(image_name, None)
        if not image_ref:
            LOG.exception(_LE('get image %s from azure mapping failed'),
                          image_name)
            ex = exception.ImageAzureMappingNotFound(image_name=image_name)
            raise ex
        LOG.debug("Get image mapping:{}".format(image_ref))
        return image_ref

    def _get_size_from_flavor(self, flavor):
        flavor_name = flavor.get('name')
        vm_size = constant.FLAVOR_MAPPING.get(flavor_name, None)
        if not vm_size:
            LOG.exception(_LE('get flavor %s from azure mapping failed'),
                          flavor_name)
            ex = exception.FlavorAzureMappingNotFound(
                flavor_name=flavor_name)
            msg = six.text_type(ex)
            LOG.exception(msg)
            raise ex
        LOG.debug("Get size mapping:{}".format(vm_size))
        return vm_size

    def _prepare_os_profile(self, instance, storage_profile, admin_password):

        os_type = None
        # 1 from volume, customized image or snapshot
        if 'image_reference' not in storage_profile and \
                        'fromImage' == storage_profile['os_disk'][
                    'create_option']:
            os_type = storage_profile['os_disk']['os_type']

        # 2 from azure marketplace image
        elif 'image_reference' in storage_profile:
            image_offer = storage_profile['image_reference']['offer']
            if image_offer in LINUX_OFFER:
                os_type = LINUX_OS
            elif image_offer in WINDOWS_OFFER:
                os_type = WINDOWS_OS
            else:
                ex = exception.OSTypeNotFound(os_type=image_offer)
                msg = six.text_type(ex)
                LOG.error(msg)
                raise ex

        os_profile = dict(computer_name=instance.hostname,
                          admin_username=USER_NAME)

        if os_type == LINUX_OS:
            key_data = instance.get('key_data')
            if key_data is not None:
                key_data = six.text_type(key_data)
                os_profile['linux_configuration'] = {
                    'ssh': {
                        'public_keys': [
                            {
                                'path': '/home/' + USER_NAME +
                                        '/.ssh/authorized_keys',
                                'key_data': key_data
                            }
                        ]
                    }
                }
            else:
                os_profile['admin_password'] = admin_password
        else:
            os_profile['admin_password'] = admin_password
        instance.os_type = os_type
        instance.save()
        return os_profile

    def _create_vm_parameters(self, storage_profile, vm_size,
                              network_profile, os_profile):
        """Create the VM parameters structure, including all info to create

        an instance.
        """
        vm_parameters = {
            'location': CONF.azure.location,
            'os_profile': os_profile,
            'hardware_profile': {
                'vm_size': vm_size
            },
            'storage_profile': storage_profile,
            'network_profile': network_profile,
        }

        # if boot from user create azure image, os_profile is not needed,
        # and all user data are the same as image's original vm.
        if not os_profile:
            del vm_parameters['os_profile']
        LOG.debug("Create vm parameters:{}".format(vm_parameters))
        return vm_parameters

    def _prepare_storage_profile(self, context, image_meta,
                                 instance, block_device_info):
        # case1 boot from volume(or boot from image to volume).
        if not instance.get('image_ref'):
            raise NotImplementedError
            # LOG.debug("case1 boot from volume.")
            # device_mapping = driver.block_device_info_get_mapping(
            #     block_device_info)
            # root_device_name = \
            #     driver.block_device_info_get_root(block_device_info)
            # os_type = uri = volume_size = None
            # for disk in device_mapping:
            #     connection_info = disk['connection_info']
            #     if root_device_name == disk['mount_device']:
            #         uri = connection_info['data']['vhd_uri']
            #         volume_size = connection_info['data']['vhd_size_gb']
            #         os_type = connection_info['data']['os_type']
            #         break
            # if not (os_type and uri and volume_size):
            #     ex = nova_ex.InvalidVolume(
            #         reason='Volume must have os_type/uri/volume_size attribute'
            #                ' when boot from it!')
            #     msg = six.text_type(ex)
            #     LOG.exception(msg)
            #     raise ex
            #
            # disk_name = self._get_blob_name(instance.uuid)
            #
            # # copy volume to new disk as image for instance.
            # self._copy_blob(VOLUME_CONTAINER, disk_name, uri)
            #
            # def _wait_for_copy():
            #     """Called at an copy until finish."""
            #     copy = self.blob.get_blob_properties(
            #         VOLUME_CONTAINER, disk_name)
            #     state = copy.properties.copy.status
            #     if state == 'success':
            #         LOG.info(_LI("Copied volume disk to new blob: %s in"
            #                      " Azure."), disk_name)
            #         raise loopingcall.LoopingCallDone()
            #     else:
            #         LOG.info(_LI(
            #             'copy volume disk: %(disk)s in Azure Progress '
            #             '%(progress)s'),
            #             dict(disk=disk_name,
            #                  progress=copy.properties.copy.progress))
            #
            # timer = loopingcall.FixedIntervalLoopingCall(_wait_for_copy)
            # timer.start(interval=0.5).wait()
            #
            # volume_blbo_name = uri.split('/')[-1]
            # try:
            #     self._delete_blob(VOLUME_CONTAINER, volume_blbo_name)
            # except Exception as e:
            #     LOG.exception(_LE("Unable to delete volume blob"
            #                       " %(disk)s in Azure because %(reason)s"),
            #                   dict(disk=volume_blbo_name,
            #                        reason=six.text_type(e)))
            #     raise e
            # else:
            #     LOG.info(_LI("Delete volume: %s blob in"
            #              " Azure"), volume_blbo_name)
            #
            # image_uri = self.blob.make_blob_url(VOLUME_CONTAINER, disk_name)
            #
            # storage_profile = {
            #     'os_disk': {
            #         'name': instance.uuid,
            #         'caching': 'None',
            #         'create_option': 'fromImage',
            #         'image': {'uri': image_uri},
            #         'vhd': {'uri': uri},
            #         'os_type': os_type
            #     }
            # }
            # disk_size_gb = instance.flavor.root_gb
            # # azure don't allow reduce size.
            # if disk_size_gb > volume_size:
            #     storage_profile['os_disk']['disk_size_gb'] = disk_size_gb

        else:
            LOG.debug("case2/3 boot from image.")

            # boot from normal openstack images, mapping to  azure marketplace
            #  or customized image, which has been uploaded to azure.
            image_reference = self._get_image_from_mapping(image_meta)
            disk_name = self._get_name_from_id(INSTANCE_PREFIX,
                                                   instance.uuid)
            storage_profile = {
                'os_disk': {
                    'name': disk_name,
                    'caching': 'None',
                    'create_option': 'fromImage',
                }
            }

            # case2 boot from customized images
            if 'uri' in image_reference:
                LOG.debug("case2 boot from customized images.")
                storage_profile['os_disk']['image'] = {
                    'uri': image_reference['uri']
                }
                storage_profile['os_disk']['os_type'] = \
                    image_reference['os_type']
            # case3 boot from azure marketplace images
            else:
                LOG.debug("case3 boot from marketplace images.")
                storage_profile['image_reference'] = image_reference

        return storage_profile

    def _attach_block_device(self, context, instance, block_device_info):
        block_device_mapping = []
        if block_device_info is not None:
            block_device_mapping = driver.block_device_info_get_mapping(
                block_device_info)
        if block_device_mapping:
            msg = "Block device information present: %s" % block_device_info
            LOG.debug(msg, instance=instance)

            root_device_name = \
                driver.block_device_info_get_root(block_device_info)
            for disk in block_device_mapping:
                connection_info = disk['connection_info']
                # if non root device, do attach. root device will automatic
                # attach when boot.
                if root_device_name != disk['mount_device']:
                    self.attach_volume(context, connection_info,
                                       instance, None)

    def _is_booted_from_volume(self, instance, disk_mapping=None):
        """Determines whether the VM is booting from volume

        Determines whether the disk mapping indicates that the VM
        is booting from a volume.
        """
        return not bool(instance.get('image_ref'))

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        if not self._check_password(admin_password):
            ex = exception.PasswordInvalid(instance_uuid=instance.uuid)
            msg = six.text_type(ex)
            LOG.error(msg)
            raise ex
        instance_uuid = instance.uuid
        try:
            vm_size = self._get_size_from_flavor(instance.get_flavor())
            network_profile = self._prepare_network_profile(instance_uuid)
            storage_profile = self._prepare_storage_profile(
                context, image_meta, instance, block_device_info)
            os_profile = self._prepare_os_profile(
                instance, storage_profile, admin_password)
            vm_parameters = self._create_vm_parameters(
                storage_profile, vm_size, network_profile, os_profile)

            self._create_update_instance(instance, vm_parameters)
            LOG.info(_LI("Create Instance in Azure Finish."),
                     instance=instance)

            self._attach_block_device(context, instance, block_device_info)
            self._delete_boot_from_volume_tmp_blob(instance)

        except Exception as e:
            LOG.exception(_LE("Instance Spawn failed, start cleanup instance"),
                          instance=instance)
            try:
                # cleanup instance related resources if instance create failed.
                self._cleanup_instance(instance)
            except Exception:
                LOG.exception(_LE("clean up in azure for failed."),
                              instance=instance)
            self._delete_boot_from_volume_tmp_blob(instance)

            # raise spawn exception
            msg = six.text_type(e)
            LOG.exception(msg)
            raise e

    def _delete_boot_from_volume_tmp_blob(self, instance):
        # boot from volume, delete tmp blob after spawn.
        if not instance.get('image_ref'):
            try:
                self._delete_blob(VOLUME_CONTAINER, instance.uuid)
            except Exception as e:
                LOG.warning(_LW("Unable to delete volume tmp blob"
                                " %(disk)s in Azure because %(reason)s"),
                            dict(disk=instance.uuid,
                                 reason=six.text_type(e)))
            else:
                LOG.info(_LI("Delete volume tmp lv: %s blob in"
                             " Azure"), instance.uuid)

    def _get_instance(self, instance_uuid):
        try:
            vm = self.compute.virtual_machines.get(
                CONF.azure.resource_group, instance_uuid)
        except exception.AzureMissingResourceHttpError:
            ex = nova_ex.InstanceNotFound(instance_id=instance_uuid)
            msg = six.text_type(ex)
            LOG.exception(msg)
            raise ex
        except Exception as e:
            ex = exception.InstanceGetFailure(reason=six.text_type(e),
                                              instance_uuid=instance_uuid)
            msg = six.text_type(ex)
            LOG.exception(msg)
            raise ex
        return vm

    def _create_update_instance(self, instance, vm_parameters):
        try:
            async_vm_action = self.compute.virtual_machines.create_or_update(
                CONF.azure.resource_group, instance.uuid, vm_parameters)
            LOG.debug("Calling Create/Update Instance in Azure "
                      "...", instance=instance)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Create/Update Instance in Azure"
                         " Finish."), instance=instance)
        except exception.AzureMissingResourceHttpError:
            ex = nova_ex.InstanceNotFound(instance_id=instance.uuid)
            msg = six.text_type(ex)
            LOG.exception(msg)
            raise ex
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.InstanceCreateUpdateFailure(
                reason=msg, instance_uuid=instance.uuid)
            raise ex

    def _get_name_from_id(self, prefix, resource_id):
        return '{}-{}'.format(prefix, resource_id)

    def _copy_disk(self, disk_name, source_id, size=None):
        disk_dict = {
            'location': CONF.azure.location,
            'creation_data': {
                'create_option': 'Copy',
                'source_uri': source_id
            }
        }
        if size:
            disk_dict['disk_size_gb'] = size
        try:
            async_action = self.disks.create_or_update(
                CONF.azure.resource_group,
                disk_name,
                disk_dict
            )
            async_action.result()
        except Exception as e:
            message = (_("Copy disk %(blob_name)s from %(source_id)s in Azure"
                         " failed. reason: %(reason)s")
                       % dict(disk_name=disk_name, source_id=source_id,
                              reason=six.text_type(e)))
            LOG.exception(message)
            raise exception.DiskCopyFailure(data=message)

    # def _copy_blob(self, container, blob_name, source_uri):
    #     try:
    #         self.blob.copy_blob(container, blob_name, source_uri)
    #     except exception.AzureMissingResourceHttpError:
    #         ex = exception.BlobNotFound(blob_name=blob_name)
    #         msg = six.text_type(ex)
    #         LOG.exception(msg)
    #         raise ex
    #     except Exception as e:
    #         msg = six.text_type(e)
    #         LOG.exception(msg)
    #         ex = exception.BlobCopyFailure(reason=msg,
    #                                        blob_name=blob_name,
    #                                        source_blob=source_uri)
    #         raise ex

    def _delete_disk(self, disk_name):
        try:
            self.disks.delete(CONF.azure.resource_group, disk_name)
        except exception.AzureMissingResourceHttpError:
            # refer lvm driver, if volume to delete doesn't exist, return True.
            message = (_LI("Volume disk: %s does not exist.") % disk_name)
            LOG.info(message)
            return True
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.DiskDeleteFailure(reason=msg,
                                             disk_name=disk_name)
            raise ex

    def _cleanup_instance(self, instance):
        """for all cleanup methods, if resources were not found, just log

        in warn, no raise, just cleanup in silent mode.
        """
        # 1 clean os disk vhd
        # vm = self._get_instance(instance.uuid)
        # os_blob_uri = vm.storage_profile.os_disk.vhd.uri
        # os_blob_name = instance.uuid
        disk_name = self._get_name_from_id(instance.uuid)
        try:
            self._delete_disk(disk_name)
            LOG.info(_LI("Delete instance's Volume"), instance=instance)
        except Exception as e:
            LOG.warning(_LW("Unable to delete blob for instance"
                            " %(instance_uuid)s in Azure because %(reason)s"),
                        dict(instance_uuid=instance.uuid,
                             reason=six.text_type(e)))

        # 2 clean network interface
        try:
            async_vm_action = self.network.network_interfaces.delete(
                CONF.azure.resource_group, instance.uuid
            )
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Delete instance's Interface"), instance=instance)
        except Exception as e:
            LOG.warning(_LW("Unable to delete network interface for instance"
                            " %(instance_uuid)s in Azure because %(reason)s"),
                        dict(instance_uuid=instance.uuid,
                             reason=six.text_type(e)))

    def destroy(self, context, instance, network_info, block_device_info=None,
                destroy_disks=True, migrate_data=None):
        LOG.debug("Calling Delete Instance in Azure ...", instance=instance)
        try:
            async_vm_action = self.compute.virtual_machines.delete(
                CONF.azure.resource_group, instance.uuid)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Delete Instance in Azure Finish."),
                     instance=instance)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = exception.InstanceDeleteFailure(
                reason=msg,
                instance_uuid=instance.uuid)
            raise ex
        self._cleanup_instance(instance)
        LOG.info(_LI("Delete and Clean Up Instance in Azure Finish."),
                 instance=instance)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        try:
            async_vm_action = self.compute.virtual_machines.restart(
                CONF.azure.resource_group, instance.uuid)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Restart Instance in Azure Finish."),
                     instance=instance)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = nova_ex.InstanceRebootFailure(reason=msg)
            raise ex

    def power_off(self, instance, timeout=0, retry_interval=0):
        try:
            async_vm_action = self.compute.virtual_machines.power_off(
                CONF.azure.resource_group, instance.uuid)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Power off Instance in Azure Finish."),
                     instance=instance)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = nova_ex.InstancePowerOffFailure(reason=msg)
            raise ex

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        try:
            async_vm_action = self.compute.virtual_machines.start(
                CONF.azure.resource_group, instance.uuid)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Power On Instance in Azure Finish."),
                     instance=instance)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = nova_ex.InstancePowerOnFailure(reason=msg)
            raise ex

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                recreate=False, block_device_info=None,
                preserve_ephemeral=False):

        try:
            async_vm_action = self.compute.virtual_machines.redeploy(
                CONF.azure.resource_group, instance.uuid)
            instance.task_state = task_states.REBUILD_SPAWNING
            instance.save()
            LOG.debug("Calling Rebuild Instance in Azure"
                      " ...", instance=instance)
            async_vm_action.wait(CONF.azure.async_timeout)
            LOG.info(_LI("Rebuild Instance in Azure Finish."),
                     instance=instance)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            ex = nova_ex.InstanceDeployFailure(reason=msg)
            raise ex

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance,
                         block_device_info=None, power_on=True):
        # nothing need to do.
        pass

    def _get_new_size(self, instance, flavor):
        """get size from mapping, return None if no mapping match."""
        sizes = self.compute.virtual_machines.list_available_sizes(
            CONF.azure.resource_group, instance.uuid)
        try:
            vm_size = self._get_size_from_flavor(flavor)
        except exception.FlavorAzureMappingNotFound:
            return None
        else:
            for i in sizes:
                if vm_size == i.name:
                    LOG.debug('Resize Instance, get new size %s',
                              vm_size)
                    return i.name
            LOG.error(_LE('Resize Instance, size %s invalid in Azure'),
                      vm_size)
            return None

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   flavor, network_info,
                                   block_device_info=None,
                                   timeout=0, retry_interval=0):
        size_obj = self._get_new_size(instance, flavor)
        # can't find new flavor in azure flavor mapping, raise.
        if not size_obj:
            e = exception.FlavorInvalid(flavor)
            msg = six.text_type(e)
            LOG.error(msg)
            raise e
        vm = self._get_instance(instance.uuid)
        vm.hardware_profile.vm_size = size_obj
        self._create_update_instance(instance, vm)
        LOG.info(_LI('Resized Instance in Azure.'), instance=instance)
        return True

    def get_volume_connector(self, instance):
        # nothing need to do with volume
        props = dict()
        props['platform'] = 'azure'
        props['ip'] = CONF.my_ip
        props['host'] = CONF.host
        return props

    def confirm_migration(self, migration, instance, network_info):
        # nothing need to do with volume
        pass

    def _check_password(self, password):
        """Check password according to azure's specification.

        8~72 charaters.
        :param password: password to set for a instance.
        :return: True or False, True for passed, False for failed.
        """
        rule = re.compile(constant.password_regex)
        if not rule.match(password):
            return False
        # disallow password from azure guide, yes, it's hard code.
        disallowed = constant.password_disallowed
        return password not in disallowed

    def attach_volume(self, context, connection_info, instance, mountpoint,
                      disk_bus=None, device_type=None, encryption=None):
        """Attach volume, append volume info into vm parameters."""
        data = connection_info['data']
        vm = self._get_instance(instance.uuid)
        data_disks = vm.storage_profile.data_disks
        luns = [i.lun for i in data_disks]
        new_lun = 1
        # azure allow upto 16 extra datadisk, 1 os disk + 1 ephemeral disk
        # ephemeral disk will always be sdb for linux.
        for i in range(1, 16):
            if i not in luns:
                new_lun = i
                break
        else:
            msg = 'Can not attach volume, exist volume amount upto 16.'
            LOG.error(msg)
            raise nova_ex.NovaException(msg)
        disk = self.disks.get(CONF.azure.resource_group, data['disk_name'])
        managed_disk = dict(id=disk.id)
        data_disk = dict(lun=new_lun,
                         name=data['disk_name'],
                         managed_disk=managed_disk,
                         create_option='attach')
        data_disks.append(data_disk)
        self._create_update_instance(instance, vm)
        LOG.info(_LI("Attach Volume to Instance in Azure finish"),
                 instance=instance)

    def detach_volume(self, connection_info, instance, mountpoint,
                      encryption=None):
        """Dettach volume, remove volume info from vm parameters."""
        vhd_name = connection_info['data']['disk_name']
        vm = self._get_instance(instance.uuid)
        data_disks = vm.storage_profile.data_disks
        not_found = True
        for i in range(len(data_disks)):
            if vhd_name == data_disks[i].name:
                del data_disks[i]
                not_found = False
                break
        if not_found:
            LOG.info(_LI('Volume: %s was not attached to Instance!'),
                     vhd_name, instance=instance)
            return
        self._create_update_instance(instance, vm)
        LOG.info(_LI("Detach Volume to Instance in Azure finish"),
                 instance=instance)

    # def snapshot(self, context, instance, image_id, update_task_state):
    #     # TODO(haifeng) when delete snapshot in glance, snapshot blob still
    #     # in azure
    #     # delete residual snapshots
    #     self._cleanup_deleted_snapshots(context)
    #     update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
    #     snapshot = self._image_api.get(context, image_id)
    #     snapshot_name = self._get_snapshot_blob_name_from_id(snapshot['id'])
    #     snapshot_url = self.blob.make_blob_url(SNAPSHOT_CONTAINER,
    #                                            snapshot_name)
    #     vm_osdisk_url = self.blob.make_blob_url(
    #         VHDS_CONTAINER, self._get_blob_name(instance.uuid))
    #     metadata = {'is_public': False,
    #                 'status': 'active',
    #                 'name': snapshot['name'],
    #                 'disk_format': 'vhd',
    #                 'container_format': 'bare',
    #                 'properties': {'azure_type': AZURE,
    #                                'azure_uri': snapshot_url,
    #                                'azure_os_type': instance.os_type,
    #                                'kernel_id': instance.kernel_id,
    #                                'image_location': 'snapshot',
    #                                'image_state': 'available',
    #                                'owner_id': instance.project_id,
    #                                'ramdisk_id': instance.ramdisk_id,
    #                                }
    #                 }
    #     self._copy_blob(SNAPSHOT_CONTAINER, snapshot_name, vm_osdisk_url)
    #     LOG.info(_LI("Calling copy os disk in "
    #                  "Azure..."), instance=instance)
    #     update_task_state(task_state=task_states.IMAGE_UPLOADING,
    #                       expected_state=task_states.IMAGE_PENDING_UPLOAD)
    #
    #     def _wait_for_copy():
    #         """Called at an copy until finish."""
    #         copy = self.blob.get_blob_properties(SNAPSHOT_CONTAINER,
    #                                              snapshot_name)
    #         state = copy.properties.copy.status
    #
    #         if state == 'success':
    #             LOG.info(_LI("Copied osdisk to new blob: %(snapshot_name)s for"
    #                          " instance: %(instance)s in Azure."),
    #                      {'snapshot_name': snapshot_name,
    #                       'instance': instance.uuid})
    #             raise loopingcall.LoopingCallDone()
    #         else:
    #             LOG.debug(
    #                 'copy os disk: {} in Azure Progress '
    #                 '{}'.format(snapshot_name, copy.properties.copy.progress))
    #
    #     timer = loopingcall.FixedIntervalLoopingCall(_wait_for_copy)
    #     timer.start(interval=0.5).wait()
    #
    #     LOG.info(_LI('Created Image from Instance: %s in'
    #                  ' Azure.'), instance.uuid)
    #     self._image_api.update(context, image_id, metadata, 'Azure image')
    #     LOG.info(_LI("Update image for snapshot image."), instance=instance)

    def resume_state_on_host_boot(self, context, instance, network_info,
                                  block_device_info=None):
        pass

    def delete_instance_files(self, instance):
        self._cleanup_instance(instance)
        return True

    def _get_snapshot_blob_name_from_id(self, blob_id):
        return '{}-{}.{}'.format(SNAPSHOT_PREFIX, blob_id, VHD_EXT)

    # def _cleanup_deleted_snapshots(self, context):
    #     """cleanup deleted resources in silent mode"""
    #     try:
    #         images = self._image_api.get_all(context)
    #         image_ids = [self._get_name_from_id(IMAGE_PREFIX, i['id']) for i in
    #                      images]
    #         snapshots = self.images.list_by_resource_group(
    #             CONF.azure.resource_group)
    #     except Exception as e:
    #         LOG.warning(_LW("Unable to delete snapshot"
    #                         " in Azure because %(reason)s"),
    #                     dict(reason=six.text_type(e)))
    #         return
    #
    #     snapshot_ids = [i.name for i in snapshots]
    #     residual_ids = set(snapshot_ids) - set(image_ids)
    #     if not residual_ids:
    #         LOG.info(_LI('No residual snapshots in Azure'))
    #         return
    #     for i in residual_ids:
    #         try:
    #             self.images.delete(CONF.azure.resource_group, i)
    #         except Exception as e:
    #             LOG.warning(_LW("Unable to delete snapshot %(snapshot)s"
    #                             "in Azure because %(reason)s"),
    #                         dict(snapshot=i,
    #                              reason=six.text_type(e)))
    #         else:
    #             LOG.info(_LI('Delete residual snapshot: %s in Azure'),
    #                      i)
    #     else:
    #         LOG.info(_LI('Delete all residual snapshots in Azure'))

    def _cleanup_deleted_nics(self):
        """cleanup deleted resources in silent mode

        add residual nics into self.residual_nics list, and delete residual
        nics addded last check, inorder to avoid new created nic for instance
        spawning.
        """
        try:
            nics = self.network.network_interfaces.list(
                CONF.azure.resource_group)
        except Exception as e:
            msg = six.text_type(e)
            LOG.exception(msg)
            return
        residual_ids = [i.name for i in nics if not i.virtual_machine]
        to_delete_ids = set(self.residual_nics) & set(residual_ids)
        self.residual_nics = list(set(self.residual_nics) | set(residual_ids))
        if not to_delete_ids:
            LOG.info(_LI('No residual nic in Azure'))
            return
        for i in to_delete_ids:
            try:
                self.network.network_interfaces.delete(
                    CONF.azure.resource_group, i
                )
            except Exception as e:
                LOG.warning(_LW("Unable to delete network_interfaces "
                                "%(nic)s in Azure because %(reason)s"),
                            dict(nic=i,
                                 reason=six.text_type(e)))
            else:
                self.residual_nics.remove(i)
                LOG.info(_LI('Delete residual Nic: %s in Azure'), i)
        else:
            LOG.info(_LI('Delete all residual Nics in Azure'))

    def _is_os_disk(self, name):
        return INSTANCE_PREFIX == name[:7]

    def _cleanup_deleted_os_disks(self):
        """cleanup deleted resources in silent mode

        cleanup os disk by check properties.lease.status and
        properties.lease.state of blob.
        """
        try:
            disks = self.disks.list_by_resource_group(
                CONF.azure.resource_group)
        except Exception as e:
            LOG.warning(_LW("Unable to delete disks"
                            " in Azure because %(reason)s"),
                        dict(reason=six.text_type(e)))
            return
        # blobs is and iterable obj, although it's empty.
        if not disks:
            LOG.info(_LI('No residual Disk in Azure'))
            return
        for i in disks:
            if self._is_os_disk(i.name) and not i.owner_id:
                try:
                    self.disks.delete(CONF.azure.resource_group, i.name)
                except Exception as e:
                    LOG.warning(_LW("Unable to delete os disk %(disk)s"
                                    "in Azure because %(reason)s"),
                                dict(disk=i.name,
                                     reason=six.text_type(e)))
                else:
                    LOG.info(_LI("Delete residual os disk: %s in"
                                 " Azure"), i.name)
        else:
            LOG.info(_LI('Delete all residual disks in Azure'))
