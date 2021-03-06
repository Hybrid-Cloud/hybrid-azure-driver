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

import six
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.common.credentials import UserPassCredentials
from cinder import exception
from cinder.i18n import _LI
from oslo_config import cfg
from oslo_log import log as logging

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

volume_opts = [
    cfg.StrOpt('location',
               default='westus',
               help='Azure Datacenter Location'),
    cfg.StrOpt('resource_group',
               default='ops_resource_group',
               help='Azure Resource Group Name'),
    cfg.StrOpt('subscription_id',
               help='Azure subscription ID'),
    cfg.StrOpt('username',
               help='Auzre username of subscription'),
    cfg.StrOpt('password',
               help='Auzre password of user of subscription')
]

CONF.register_opts(volume_opts, 'azure')


class Azure(object):
    def __init__(self, username=CONF.azure.username,
                 password=CONF.azure.password,
                 subscription_id=CONF.azure.subscription_id,
                 resource_group=CONF.azure.resource_group,
                 location=CONF.azure.location):

        credentials = UserPassCredentials(username, password)
        LOG.info(_LI('Login with Azure username and password.'))
        self.compute = ComputeManagementClient(credentials,
                                               subscription_id)
        self.resource = ResourceManagementClient(credentials,
                                                 subscription_id)
        try:
            self.resource.resource_groups.create_or_update(
                CONF.azure.resource_group, {'location': location})
            LOG.info(_LI("Create/Update Resource Group"))
        except Exception as e:
            msg = six.text_type(e)
            ex = exception.VolumeBackendAPIException(reason=msg)
            LOG.exception(msg)
            raise ex
