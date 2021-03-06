import mock

from nova import test
from nova.virt.azureapi import adapter

USERNAME = 'AZUREUSER'
PASSWORD = 'PASSWORD'
SUBSCRIBE_ID = 'ID'
RG = 'RG'
SAC = 'SC'
KEY = 'KEY'


class FakeKey(object):
    class Value(object):
        value = KEY
    keys = [Value()]


class AzureTestCase(test.NoDBTestCase):

    @mock.patch('nova.virt.azureapi.adapter.UserPassCredentials')
    @mock.patch('nova.virt.azureapi.adapter.ResourceManagementClient')
    @mock.patch('nova.virt.azureapi.adapter.ComputeManagementClient')
    @mock.patch('nova.virt.azureapi.adapter.NetworkManagementClient')
    @mock.patch('nova.virt.azureapi.adapter.StorageManagementClient')
    @mock.patch('nova.virt.azureapi.adapter.CloudStorageAccount')
    def test_start_driver_with_user_password_subscribe_id(
            self, cloudstorage, storage, network,
            compute, resource, credential):
        self.flags(group='azure', username=USERNAME,
                   password=PASSWORD, subscription_id=SUBSCRIBE_ID,
                   storage_account=SAC)
        storage().storage_accounts.list_keys = mock.Mock(return_value=FakeKey)
        cloudstorage.create_page_blob_service = mock.Mock()

        azure = adapter.Azure()

        credential.assert_called_once_with(USERNAME, PASSWORD)
        resource.assert_called_once_with(credential(), SUBSCRIBE_ID)
        compute.assert_called_once_with(credential(), SUBSCRIBE_ID)
        network.assert_called_once_with(credential(), SUBSCRIBE_ID)
        storage.assert_called_with(credential(), SUBSCRIBE_ID)
        cloudstorage.assert_called_once_with(account_name=SAC, account_key=KEY)
        self.assertTrue(hasattr(azure, 'blob'))
        self.assertTrue(hasattr(azure, 'resource'))
        self.assertTrue(hasattr(azure, 'compute'))
        self.assertTrue(hasattr(azure, 'network'))
        self.assertTrue(hasattr(azure, 'storage'))
