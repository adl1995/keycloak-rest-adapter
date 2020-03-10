import requests
from authlib.jose import jwk, jwt
from authlib.jose.errors import MissingClaimError
from authlib.oidc.core import ImplicitIDToken, UserInfo
from flask import current_app, request
from flask_restplus import Resource

from utils import json_response


class ImplicitIDTokenNoNonce(ImplicitIDToken):
    """
    Don't validate the nonce claim as it's not coming with the token
    """

    ESSENTIAL_CLAIMS = ["iss", "sub", "aud", "exp", "iat"]

    # Based on https://github.com/lepture/authlib/blob/master/authlib/oidc/core/claims.py#L115
    def validate_azp(self):
        aud = self.get("aud")
        client_id = self.params.get("client_id")
        required = False
        if aud and client_id:
            if isinstance(aud, list) and len(aud) == 1:
                aud = aud[0]
            if aud != client_id:
                required = True

        azp = self.get("azp")
        if required and not azp:
            raise MissingClaimError("azp")


def validate_api_access(access_token):
    """
    Verify if the caller entity is allowed to call the API
    :param access_token: The access token that was parsed for the API
    :return: Boolean
    """
    try:
        if access_token["azp"] in current_app.config['AUTH_AUTHORIZED_APPS']:
            return True
        elif (
            current_app.config['AUTH_API_ACCESS_ROLE']
            in access_token["resource_access"][current_app.config["OIDC_CLIENT_ID"]][
                "roles"
            ]
        ):
            return True
    except Exception as e:
        current_app.logger.error(e)
    return False


def validate_user_access(access_token, username):
    """
    Verify if the caller entity is allowed to call the API
    :param access_token: The access token that was parsed for the API
    :param username: The username expected in the access token
    :return: Boolean
    """
    try:
        if (
            current_app.config['AUTH_USER_ACTIONS_ROLE']
            in access_token["resource_access"][current_app.config["OIDC_CLIENT_ID"]][
                "roles"
            ]
            and access_token["sub"] == username
        ):
            return True
    except Exception as e:
        current_app.logger.error(e)
    return False


def parse_id_token(id_token):
    """
    Parses an ID token and returns it as a set of user info claims
    :param id_token:
    :return: Boolean
    """

    def load_key(header, payload):
        jwk_set = requests.get(current_app.config["OIDC_JWKS_URL"]).json()
        return jwk.loads(jwk_set, header.get("kid"))

    claims_params = {"client_id": current_app.config["OIDC_CLIENT_ID"]}
    claims_cls = ImplicitIDTokenNoNonce
    claims_options = {"iss": {"values": [current_app.config["OIDC_ISSUER"]]}}
    claims = jwt.decode(
        id_token,
        key=load_key,
        claims_cls=claims_cls,
        claims_options=claims_options,
        claims_params=claims_params,
    )
    claims.validate(leeway=120)
    return UserInfo(claims)


def oidc_validate_api(func):
    """
    Decorator for validation of the auth token
    """

    def function_wrapper(*args, **kwargs):
        try:
            auth_header = request.headers["Authorization"]
            token = auth_header.split("Bearer")[1].strip()
            user_info = parse_id_token(token)
            current_app.logger.debug(user_info)
            if not validate_api_access(user_info):
                current_app.logger.error(
                    "User is not allowed to access the API as an administrator"
                )
                return json_response("Unauthorized", 401)
        except Exception as e:
            current_app.logger.error(f"Authentication error: {e}")
            return json_response("Unauthorized", 401)
        return func(*args, **kwargs)

    return function_wrapper


def oidc_validate_user_or_api(func):
    """
    Decorator for validation of the auth token
    """

    def function_wrapper(resource: Resource, username: str, *args, **kwargs):
        try:
            auth_header = request.headers["Authorization"]
            token = auth_header.split("Bearer")[1].strip()
            user_info = parse_id_token(token)
            current_app.logger.debug(user_info)
            user_access = validate_user_access(user_info, username)
            api_access = validate_api_access(user_info)
            if not user_access and not api_access:
                current_app.logger.error(
                    "User is not authorized to access or modify the resource"
                )
                return json_response("Unauthorized", 401)
        except Exception as e:
            current_app.logger.error(f"Authentication error: {e}")
            return json_response("Unauthorized", 401)
        return func(resource, username, *args, **kwargs)

    return function_wrapper
