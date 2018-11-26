#!/usr/bin/env python

from ConfigParser import ConfigParser
from flask import Flask, make_response
from flask_oidc import OpenIDConnect
from flask_restful import Resource, Api, request

import json
import logging
import requests
import ssl
import sys
import os


config_dir = os.getcwd()
privatekey_file = "{0}/config/keycloak-rest-adapter_nopass.key"
certificate_file = "{0}/config/keycloak-rest-adapter.crt"
keycloakclient_config_file = '{0}/config/keycloak_client.cfg'.format(
    config_dir)
flask_oidc_client_secrets_file = '{0}/config/flask_oidc_config.json'.format(
    config_dir)

API_VERSION = 1.0
API_URL_PREFIX = '/api/v%s' % API_VERSION

app = Flask(__name__)
api = Api(app)

app.config.update({
    'SECRET_KEY': 'WHATEVER',
    'TESTING': True,
    'DEBUG': True,
    'OIDC-SCOPES': ['openid'],
    'OIDC_CLIENT_SECRETS': flask_oidc_client_secrets_file,
    'OIDC_INTROSPECTION_AUTH_METHOD': 'client_secret_post',
    'OIDC_OPENID_REALM': 'master',
    'OIDC_TOKEN_TYPE_HINT': 'access_token',
    'OIDC_RESOURCE_SERVER_ONLY': True,
})

oidc = OpenIDConnect(app)


def configure_logging():
    """Logging setup
    """
    logging.basicConfig(level=logging.DEBUG)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s - %(message)s')

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # Requests logs some stuff at INFO that we don't want
    # unless we have DEBUG
    requests_log = logging.getLogger("requests")
    requests_log.setLevel(logging.ERROR)
    return logger


# To be investigated:
# https://stackoverflow.com/questions/46470477/how-to-get-keycloak-users-via-rest-without-admin-account

logger = configure_logging()


class KeycloakAPIClient(object):
    """
    KeycloakAPI Client to interact with the Keycloak API.
    """

    def __init__(self, config_file,
                 ssl_cert_path=''):
        """
        Initialize the class with the params needed to use the API.
        config_file: Path to file  with config to instanciate the Keycloak Client
        ssl_cert_path: Path to file or directory with certificates of trusted CA
        """
        config = ConfigParser()
        config.readfp(open(config_file))
        self.keycloak_server = config.get("keycloak", "server")
        self.realm = config.get("keycloak", "realm")
        self.admin_user = config.get("keycloak", "admin_user")
        self.admin_password = config.get("keycloak", "admin_password")
        self.client_id = config.get("keycloak", "keycloak-rest-adapter-client")
        self.client_secret = config.get(
            "keycloak", "keycloak-rest-adapter-client-secret")
        self.ssl_cert_path = config.get("keycloak", "ssl_cert_path")

        self.base_url = 'https://%s/auth' % (self.keycloak_server)
        self.headers = {'Content-Type': "application/x-www-form-urlencoded"}

        # Persistent SSL configuration
        # http://docs.python-requests.org/en/master/user/advanced/#ssl-cert-verification
        self.session = requests.Session()
        self.session.verify = self.ssl_cert_path

        self.access_token_object = None
        self.master_realm_client = self.get_client_by_clientID("master-realm")

    def __send_request(self, request_type, url, **kwargs):
        # if there is 'headers' in kwargs use it instead of default class one
        r_headers = self.headers.copy()
        if 'headers' in kwargs:
            r_headers.update(kwargs.pop('headers', None))

        if request_type.lower() == 'delete':
            ret = self.session.delete(url=url, headers=r_headers,
                                      **kwargs)
        elif request_type.lower() == 'get':
            ret = self.session.get(url=url, headers=r_headers,
                                   **kwargs)
        elif request_type.lower() == 'post':
            ret = self.session.post(url=url, headers=r_headers,
                                    **kwargs)
        elif request_type.lower() == 'put':
            ret = self.session.put(url=url, headers=r_headers,
                                   **kwargs)
        else:
            raise Exception("Specified request_type '%s' not supported" %
                            request_type)
        return ret

    def send_request(self, request_type, url, **kwargs):
        """ Call the private method __send_request and retry in case the access_token has expired"""
        ret = self.__send_request(request_type, url, **kwargs)

        if ret.reason == 'Unauthorized':
            logger.info("Admin token seems expired. Getting new admin token")
            self.access_token_object = self.get_admin_access_token()
            logger.info("Updating request headers with new access token")
            kwargs['headers'] = self.__get_admin_access_token_headers()
            return self.__send_request(request_type, url, **kwargs)
        else:
            return ret

    def __get_admin_access_token_headers(self):
        """
        Get HTTP headers with an admin bearer token
        """

        if self.access_token_object == None:
            # get admin access token for the 1st time
            self.access_token_object = self.get_admin_access_token()

        access_token = self.access_token_object['access_token']
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer {0}'.format(access_token)
        }

        return headers

    def set_client_fine_grain_permission(self, clientid, status):
        """
        Enable/disable fine grain permissions for the given client
        clientid: ID string of the client. E.g: 6781736b-e1f7-4ff7-a883-f4168c4dbd8a
        status: boolean value to enable/disable permissions
        """
        logger.info(
            "Setting client '%s' fine grain permissions to '%s'", clientid, status)
        headers = self.__get_admin_access_token_headers()
        data = {'enabled': status}
        url = '{0}/admin/realms/{1}/clients/{2}/management/permissions'.format(
            self.base_url, self.realm, clientid)

        ret = self.send_request(
            'put',
            url,
            headers=headers,
            data=json.dumps(data))
        return ret

    def get_client_by_clientID(self, client_id):
        """
        Get the list of clients that match the given clientID name
        """
        headers = self.__get_admin_access_token_headers()
        payload = {'clientId': client_id,
                   'viewable': True
                   }
        url = '{0}/admin/realms/{1}/clients'.format(
            self.base_url, self.realm)

        ret = self.send_request(
            'get',
            url,
            headers=headers,
            params=payload)

        logger.info("Getting client '%s' object", client_id)
        client = json.loads(ret.text)

        # keycloak returns a list of 1 element if found, empty if not
        if len(client) == 1:
            logger.info("Found client '%s' (%s)", client_id, client[0]['id'])
            return client[0]
        else:
            logger.info("Client '%s' NOT found", client_id)
            return client

    def get_client_policy_by_name(self, policy_name):
        """
        Get the list of client policies that match the given policy name
        """
        logger.info("Getting policy '%s' object", policy_name)
        headers = self.__get_admin_access_token_headers()
        payload = {'name': policy_name}
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/policy'.format(
            self.base_url, self.realm, self.master_realm_client['id'])

        ret = self.send_request(
            'get',
            url,
            headers=headers,
            params=payload)

        # keycloak returns a list of all matching policies
        matching_policies = json.loads(ret.text)
        # return exact match
        return [policy for policy in matching_policies
                if policy['name'] == policy_name]

    def create_client_policy(self, clientid, policy_name, policy_description="",
                             policy_logic="POSITIVE", policy_strategy="UNANIMOUS"):
        """
        Create client policy for the given clientid
        clientid: ID string of the client. E.g: 6781736b-e1f7-4ff7-a883-f4168c4dbd8a
        """
        logger.info("Creating policy new '%s' for client %s",
                    policy_name, clientid)
        headers = self.__get_admin_access_token_headers()
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/policy/client'.format(
            self.base_url, self.realm, self.master_realm_client['id'])

        logger.info("Checking if '%s' already exists...", policy_name)
        client_policy = self.get_client_policy_by_name(policy_name)

        if len(client_policy) == 0:
            # create new policy
            logger.info(
                "It does not exist. Creating new policy and subscribing it to client '%s'", clientid)
            http_method = 'post'
            subscribed_clients = [clientid]

        else:
            # update already existing policy
            logger.info(
                "There is an exisintg policy with name %s. Updating it to subscribe client '%s'", policy_name, clientid)
            url = url + "/{0}".format(client_policy[0]['id'])
            http_method = 'put'
            subscribed_clients = json.loads(
                client_policy[0]['config']['clients'])
            subscribed_clients.append(clientid)

        data = {"clients": subscribed_clients,
                "name": policy_name,
                "type": "client",
                "description": policy_description,
                "logic": policy_logic,
                "decisionStrategy": policy_strategy
                }

        ret = self.send_request(
            http_method,
            url,
            headers=headers,
            data=json.dumps(data))
        return ret

    def get_auth_permission_by_name(self, permission_name):
        """
        Get REALM's authorization permission by name
        permission_name: authorization permission name to get
        ret: Matching Authorization permission object
        """
        logger.info("Getting authorization permission '%s' object",
                    permission_name)
        headers = self.__get_admin_access_token_headers()
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/permission/'.format(
            self.base_url, self.realm, self.master_realm_client['id'])

        payload = {'name': permission_name}
        ret = self.send_request(
            'get',
            url,
            headers=headers,
            params=payload)
        return json.loads(ret.text)

    def get_auth_policy_by_name(self, policy_name):
        """
        Get REALM's authorization policies by name
        policy_name: authorization policy name to get
        ret: Matching Authorization policy object
        """
        logger.info("Getting authorization policy '%s'", policy_name)
        headers = self.__get_admin_access_token_headers()
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/policy/'.format(
            self.base_url, self.realm, self.master_realm_client['id'])

        payload = {'name': policy_name}
        ret = self.send_request(
            'get',
            url,
            headers=headers,
            params=payload)
        return ret

    def get_client_token_exchange_permission(self, clientid):
        """
        Get token-exchange permission for the client with given ID
        clientid: ID string of the client. E.g: 6781736b-e1f7-4ff7-a883-f4168c4dbd8a
        """
        logger.info(
            "Getting token-exhange permission for client '%s'...", clientid)
        token_exchange_permission_name = "token-exchange.permission.client.{0}".format(
            clientid)
        return self.get_auth_permission_by_name(token_exchange_permission_name)[0]

    def grant_token_exchange_permissions(self, target_clientid, requestor_clientid):
        """
        Grant token-exchange permission for target client to destination client
        target_clientid: ID string of the target client. E.g: 6781736b-e1f7-4ff7-a883-f4168c4dbd8a
        requestor_clientid: ID string of the client to exchange its token for target_clientid E.g: 6781736b-e1f7-4ff7-a883-f4168c4dbd8a
        """
        self.set_client_fine_grain_permission(target_clientid, True)
        client_token_exchange_permission = self.get_client_token_exchange_permission(
            target_clientid)
        tep_associated_policies = self.get_permission_associated_policies(
            client_token_exchange_permission['id'])
        policies = [policy['id'] for policy in tep_associated_policies]

        policy_name = "allow token exchange for {0}".format(
            requestor_clientid)
        policy_description = "Allow token exchange for '{0}' client".format(
            requestor_clientid)

        self.create_client_policy(
            requestor_clientid,
            policy_name,
            policy_description)
        policy = self.get_client_policy_by_name(policy_name)[0]

        headers = self.__get_admin_access_token_headers()
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/permission/scope/{3}'.format(
            self.base_url, self.realm, self.master_realm_client['id'], client_token_exchange_permission['id'])

        # if permission associated with at least one policy -->  decisionStrategy to AFFIRMATIVE instead of UNANIMOUS
        if len(policies) > 0:
            client_token_exchange_permission['decisionStrategy'] = "AFFIRMATIVE"

        client_token_exchange_permission['policies'] = policies
        client_token_exchange_permission['policies'].append(policy['id'])
        logger.info("Granting token-exhange between client '%s' and '%s'",
                    target_clientid, requestor_clientid)
        ret = self.send_request(
            'put',
            url,
            headers=headers,
            data=json.dumps(client_token_exchange_permission))
        return ret

    def get_permission_associated_policies(self, permission_id):
        url = '{0}/admin/realms/{1}/clients/{2}/authz/resource-server/policy/{3}/associatedPolicies'.format(
            self.base_url, self.realm, self.master_realm_client['id'], permission_id)
        headers = self.__get_admin_access_token_headers()
        ret = self.send_request(
            'get',
            url,
            headers=headers)
        return json.loads(ret.text)

    def get_all_clients(self):
        """
        Return list of clients
        """
        logger.info("Getting all clients")
        headers = self.__get_admin_access_token_headers()
        payload = {'viewableOnly': 'true'}
        url = '{0}/admin/realms/{1}/clients'.format(
            self.base_url, self.realm)
        ret = self.send_request(
            'get',
            url,
            headers=headers,
            params=payload)
        # return clients as list of json instead of string
        return json.loads(ret.text)

    def refresh_admin_token(self, admin_token):
        """
        https://www.keycloak.org/docs/2.5/server_development/topics/admin-rest-api.html
        """
        logger.info("Refreshing admin access token")
        grant_type = "refresh_token"
        refresh_token = access_token_object['refresh_token']

        url = '{0}/realms/{1}/protocol/openid-connect/token'.format(
            self.base_url, self.realm)
        payload = "refresh_token={0}&grant_type={1}&username={2}&password={3}".format(
            refresh_token, grant_type, self.admin_user, self.admin_password)
        ret = self.send_request('post', url, data=payload)
        return json.loads(ret.tex)

    def get_admin_access_token(self):
        """
        https://www.keycloak.org/docs/2.5/server_development/topics/admin-rest-api.html
        """
        logger.info("Getting admin access token")

        client_id = "admin-cli"
        grant_type = "password"

        url = '{0}/realms/{1}/protocol/openid-connect/token'.format(
            self.base_url, self.realm)
        payload = "client_id={0}&grant_type={1}&username={2}&password={3}".format(
            client_id, grant_type, self.admin_user, self.admin_password)
        ret = self.send_request('post', url, data=payload)
        return json.loads(ret.text)

    def get_access_token(self):
        """ Return access_token after performing Client Credentials grant request and
            Authorization Code Exchange
        """
        access_token_object = self.get_client_credentials_access_token(
            self.client_id, self.client_secret)
        subject_token = access_token_object['access_token']
        audience = "authorization-service-api-dev-danielfr"
        client_exchange_token_object = self.get_token_exchange_request(
            self.client_id, self.client_secret, subject_token, audience)
        access_token = client_exchange_token_object['access_token']
        return access_token

    def get_client_credentials_access_token(self, client_id, client_secret):
        """ Return the access_token JSON object requested -> oauth2 Client Credentials grant.
        https://www.oauth.com/oauth2-servers/access-tokens/client-credentials/
        """
        grant_type = "client_credentials"

        url = '{0}/realms/{1}/protocol/openid-connect/token'.format(
            self.base_url, self.realm)
        payload = "client_id={0}&grant_type={1}&client_secret={2}".format(
            client_id, grant_type, client_secret)
        r = self.send_request('post', url, data=payload)
        return json.loads(r.text)

    def get_token_exchange_request(
            self, client_id, client_secret, subject_token, audience):
        """ Return an Authorization Code Exchange token JSON object -> oauth2 Authorization Code Exchange
        https://www.oauth.com/oauth2-servers/pkce/authorization-code-exchange/
        """
        grant_type = "urn:ietf:params:oauth:grant-type:token-exchange"
        subject_token_type = "urn:ietf:params:oauth:token-type:access_token"

        url = '{0}/realms/{1}/protocol/openid-connect/token'.format(
            self.base_url, self.realm)
        payload = "client_id={0}&grant_type={1}&client_secret={2}&subject_token_type={3}&subject_token={4}&audience={5}".format(
            client_id, grant_type, client_secret, subject_token_type, subject_token, audience)
        r = self.send_request('post', url, data=payload)
        return json.loads(r.text)

    def __create_client(self, access_token, **kwargs):
        """Private method for adding a new client.
        access_token: https://www.oauth.com/oauth2-servers/access-tokens/access-token-response/
        #_clientrepresentation
        kwargs: See the full list of available params: https://www.keycloak.org/docs-api/3.4/rest-api/index.html
        """
        headers = {
            'Content-Type': 'application/json',
            'Authorization': '{0}'.format(access_token)
        }
        url = '{0}/realms/{1}/clients-registrations/default'.format(
            self.base_url, self.realm)
        return self.send_request(
            'post',
            url,
            headers=headers,
            data=json.dumps(kwargs))

    def create_new_openid_client(self, **kwargs):
        """Add new OPENID client.
        kwargs: See the full list of available params: https://www.keycloak.org/docs-api/3.4/rest-api/index.html#_clientrepresentation
        """
        access_token = self.get_access_token()
        # load minimum default values to create OPENID-CONNECT client
        if 'redirectUris' not in kwargs:
            kwargs['redirectUris'] = []
        if 'attributes' not in kwargs:
            kwargs['attributes'] = {}
        if 'protocol' not in kwargs or kwargs['protocol'] != 'openid-connect':
            kwargs['protocol'] = 'openid-connect'
        return self.__create_client(access_token, **kwargs)

    def create_new_saml_client(self, **kwargs):
        """Add new SAML client.
        kwargs: See the full list of available params: https://www.keycloak.org/docs-api/3.4/rest-api/index.html#_clientrepresentation
        """
        access_token = self.get_access_token()
        # load minimum default values to create SAML client
        if 'redirectUris' not in kwargs:
            kwargs['redirectUris'] = []
        if 'attributes' not in kwargs:
            kwargs['attributes'] = {}
        if 'protocol' not in kwargs or kwargs['protocol'] != 'saml':
            kwargs['protocol'] = 'saml'
        return self.__create_client(access_token, **kwargs)

    def create_new_client(self, **kwargs):
        """Add new client.
        kwargs: See the full list of available params: https://www.keycloak.org/docs-api/3.4/rest-api/index.html#_clientrepresentation
        """
        if 'protocol' in kwargs:
            protocol = kwargs['protocol']
            if protocol == 'saml':
                return self.create_new_saml_client(**kwargs)
            elif protocol == 'openid-connect':
                return self.create_new_openid_client(**kwargs)
            else:
                return json_response(
                    "The request is invalid. 'protocol' only supports 'saml' and 'openid-connect' values",
                    400)
        else:
            return json_response(
                "The request is missing the 'protocol'. It must be passed as a query parameter",
                400)


##################################
# not very elengant, see ->
# https://stackoverflow.com/questions/25925217/object-oriented-python-with-flask-server/25925286

keycloak_client = KeycloakAPIClient(keycloakclient_config_file)


def json_response(data='', status=200, headers=None):
    JSON_MIME_TYPE = 'application/json'
    headers = headers or {}
    if 'error' or 'error_description' in data:
        status = 400
    if 'Content-Type' not in headers:
        headers['Content-Type'] = JSON_MIME_TYPE
    return make_response(data, status, headers)


def get_request_data(request):
    # https://stackoverflow.com/questions/10434599/how-to-get-data-received-in-flask-request/25268170
    return request.form.to_dict() if request.form else request.get_json()


class Client(Resource):

    @app.route('{0}/client/token-exchange-permissions'.format(API_URL_PREFIX), methods=['POST'])
    def client_token_exchange_permissions():
        data = get_request_data(request)
        if not data or 'target' not in data or 'requestor' not in data:
            return json_response(
                "The request is missing 'target' or 'requestor'. They must be passed as a query parameter",
                400)
        target_client_name = data['target']
        requestor_client_name = data['requestor']

        target_client = keycloak_client.get_client_by_clientID(
            target_client_name)
        requestor_client = keycloak_client.get_client_by_clientID(
            requestor_client_name)
        if target_client and requestor_client:
            ret = keycloak_client.grant_token_exchange_permissions(
                target_client['id'], requestor_client['id'])
            return ret.reason
        else:
            return json_response(
                "Verify '{0}' and '{1}' exist".format(
                    target_client_name, requestor_client_name),
                400)

    @app.route('{0}/client/openid'.format(API_URL_PREFIX), methods=['POST'])
    @oidc.accept_token(require_token=True)
    def client_create_openid():
        data = get_request_data(request)
        # no clientId --> return error
        if not data or 'clientId' not in data:
            return json_response(
                "The request is missing the 'clientId'. It must be passed as a query parameter",
                400)
        new_client = keycloak_client.create_new_openid_client(**data)
        return new_client.text

    @app.route('{0}/client/saml'.format(API_URL_PREFIX), methods=['POST'])
    @oidc.accept_token(require_token=True)
    def client_create_saml():
        data = get_request_data(request)
        # no clientId --> return error
        if not data or 'clientId' not in data:
            return json_response(
                "The request is missing the 'clientId'. It must be passed as a query parameter",
                400)
        new_client = keycloak_client.create_new_saml_client(**data)
        return new_client.text

    @app.route('{0}/client'.format(API_URL_PREFIX), methods=['POST'])
    @oidc.accept_token(require_token=True)
    def client_create():
        data = get_request_data(request)
        # no clientId nor protocol --> return error
        if not data or 'clientId' not in data or 'protocol' not in data:
            return json_response(
                "The request is missing the 'clientId' or 'protocol'. They must be passed as a query parameter.",
                400)
        new_client = keycloak_client.create_new_client(**data)
        return new_client.text


if __name__ == '__main__':
    print("** Debug mode should never be used in a production environment! ***")
    app.run(
        host='0.0.0.0',
        ssl_context=(
            certificate_file,
            privatekey_file),
        port=8080,
        debug=True)
