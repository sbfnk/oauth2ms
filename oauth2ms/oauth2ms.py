#!/usr/bin/env python
# Copyright 2020 Harish Krupo
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from xdg.BaseDirectory import (
        load_first_config,
        save_data_path
        )

import argparse
import webbrowser
import logging
import json
import msal

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from wsgiref import simple_server
import wsgiref.util
import sys
import uuid
import pprint
import os
import atexit
import base64
import gnupg
import io

def load_config():
    config_file = load_first_config(APP_NAME, "config.json")
    if config_file is None:
        print(f"Couldn't find configuration file. Config file must be at: $XDG_CONFIG_HOME/{APP_NAME}/config.json")
        print("Current value of $XDG_CONFIG_HOME is {}".format(os.getenv("XDG_CONFIG_HOME")))
        return None
    return json.load(open(config_file, 'r'))

def build_msal_app(config, cache = None):
    return msal.ConfidentialClientApplication(
        config['client_id'],
        authority="https://login.microsoftonline.com/" + config['tenant_id'],
        client_credential=config['client_secret'], token_cache=cache)

def get_auth_url(config, cache, state):
    return build_msal_app(config, cache).get_authorization_request_url(
        config['scopes'],
        state=state,
        redirect_uri=config["redirect_uri"]);

class WSGIRequestHandler(wsgiref.simple_server.WSGIRequestHandler):
    """Silence out the messages from the http server"""
    def log_message(self, format, *args):
        pass

class WSGIRedirectionApp(object):
    """WSGI app to handle the authorization redirect.

    Stores the request URI and displays the given success message.
    """

    def __init__(self, message):
        self.last_request_uri = None
        self._success_message = message

    def __call__(self, environ, start_response):
        start_response("200 OK", [("Content-type", "text/plain; charset=utf-8")])
        self.last_request_uri = wsgiref.util.request_uri(environ)
        return [self._success_message.encode("utf-8")]

def validate_config(config):
    conditions = [
        "tenant_id" in config,
        "client_id" in config,
        "redirect_host" in config,
        "redirect_port" in config,
        "redirect_path" in config,
        "scopes" in config,
        "client_secret" in config
    ]

    return all(conditions)

def build_new_app_state(crypt):
    cache = msal.SerializableTokenCache()
    config = load_config()
    if config is None:
        return None

    if not validate_config(config):
        print("Invalid config")
        print("Config must contain the keys: " +
            "tenant_id, client_id, redirect_host, redirect_port, " +
            "redirect_path, scopes, client_secret")
        return None

    state = str(uuid.uuid4())

    redirect_path = config["redirect_path"]
    # Remove / at the end if present, and path is not just /
    if redirect_path != "/" and redirect_path[-1] == "/":
        redirect_path = redirect_path[:-1]

    config["redirect_uri"] = "http://" + config['redirect_host'] + ":" + config['redirect_port'] + redirect_path
    auth_url = get_auth_url(config, cache, state)
    wsgi_app = WSGIRedirectionApp(SUCCESS_MESSAGE)
    http_server = simple_server.make_server(config['redirect_host'],
                                            int(config['redirect_port']),
                                            wsgi_app,
                                            handler_class=WSGIRequestHandler)
    if cmdline_args.no_browser:
        print("Please navigate to this url: " + auth_url)
    else:
        webbrowser.open(auth_url, new=2, autoraise=True)

    http_server.handle_request()

    auth_response = wsgi_app.last_request_uri
    http_server.server_close()
    parsed_auth_response = parse_qs(auth_response)

    code_key = config["redirect_uri"] + "?code"
    if code_key not in parsed_auth_response:
        return None

    auth_code = parsed_auth_response[code_key]

    result = build_msal_app(config, cache).acquire_token_by_authorization_code(
        auth_code,
        scopes=config['scopes'],
        redirect_uri=config["redirect_uri"]);


    if result.get("access_token") is None:
        print("Something went wrong during authorization")
        print("Server returned: {}".format(result))
        return None

    token = result["access_token"]
    app = {}
    app['config'] = config
    app['cache'] = cache
    app['crypt'] = crypt
    return app, token

def fetch_token_from_cache(app):
    config = app['config']
    cache = app['cache']
    cca = build_msal_app(config, cache)
    accounts = cca.get_accounts()
    if cca.get_accounts:
        result = cca.acquire_token_silent(config['scopes'], account=accounts[0])
        return result["access_token"]
    else:
        None

def build_app_state_from_credentials(crypt, credentials_file):
    # If the file is missing return None
    if not os.path.exists(credentials_file):
        return None

    credentials = open(credentials_file, "r").read()
    if crypt:
        gpg = crypt["gpg"]
        credentials = str(gpg.decrypt(credentials))

    # Make sure it is a valid json object
    try:
        config = json.loads(credentials)
    except:
        print("Not a valild json file or it is ecrypted. Maybe add/remove the -e arugment?")
        sys.exit(1)

    # We don't have a token cache?
    if config.get("token_cache") is None:
        return None

    app_state = {};
    cache = msal.SerializableTokenCache()
    cache.deserialize(config["token_cache"])
    app_state["config"] = config
    app_state["cache"] = cache
    app_state["crypt"] = crypt
    return app_state

"""
Encode the xoauth 2 message based on:
https://docs.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth#sasl-xoauth2
"""
def encode_xoauth2(app, token):
    config = app['config']
    cache = app['cache']
    cca = build_msal_app(config, cache)
    accounts = cca.get_accounts()
    username = accounts[0]["username"]
    C_A = b'\x01'
    user = ("user=" + username).encode("ascii")
    btoken = ("auth=Bearer " + token).encode("ascii")
    xoauth2_bytes = user + C_A + btoken + C_A + C_A
    return base64.b64encode(xoauth2_bytes).decode("utf-8")

def main():
    pp = pprint.PrettyPrinter(indent = 4)

    credentials_file = save_data_path("oauth2ms") + "/credentials.bin"

    _LOGGER = logging.getLogger(__name__)

    APP_NAME = "oauth2ms"
    SUCCESS_MESSAGE = "Authorization complete."

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--encode-xoauth2", action = "store_true", default = False,
        help = "Print xoauth2 encoded token instead of the plain token"
    )
    parser.add_argument(
        "-e", "--encrypt-using-fingerprint", action = "store", default = None,
        help = "Use gpg encryption to encrypt/decrypt the token cache. "
        "Argument is the fingerprint/email to be used")
    parser.add_argument(
        "--gpg-home", action = "store", default = None,
        help = "Set the gpg home directory")
    parser.add_argument(
        "--no-browser", action = "store_true", default = False,
        help = "Don't open a browser with URL. Instead print the URL. "
        "Useful inside virtualized environments like WSL.")

    cmdline_args = parser.parse_args()

    crypt = None
    if cmdline_args.encrypt_using_fingerprint:
        if cmdline_args.gpg_home:
            gpg_args = {"gnupghome": cmdline_args.gpg_home}
        gpg = gnupg.GPG(**gpg_args)
        crypt = {
            "gpg" : gpg,
            "fingerprint": cmdline_args.encrypt_using_fingerprint
        }

    token = None
    app_state = build_app_state_from_credentials(crypt, credentials_file)
    if app_state is None:
        app_state, token = build_new_app_state(crypt)

    if app_state is None:
        print("Something went wrong!")
        sys.exit(1)

    if token is None:
        token = fetch_token_from_cache(app_state)

    if token is not None:
        if cmdline_args.encode_xoauth2:
            print(encode_xoauth2(app_state, token))
        else:
            print(token);

            cache = app_state['cache']
    if cache.has_state_changed:
        config = app_state["config"]
        config["token_cache"] = cache.serialize()
        config_json = json.dumps(config)
        crypt = app_state.get("crypt")
        if crypt:
            gpg = crypt["gpg"]
            fingerprint = crypt["fingerprint"]
            config_json = str(gpg.encrypt(config_json, fingerprint))
            open(credentials_file, "w").write(config_json)

if __name__ == '__main__':
    sys.exit(main())
