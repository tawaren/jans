import contextlib
import json
import logging.config
import os
from collections import namedtuple

from jans.pycloudlib import get_manager
from jans.pycloudlib.persistence import CouchbaseClient
from jans.pycloudlib.persistence import LdapClient
from jans.pycloudlib.persistence import SpannerClient
from jans.pycloudlib.persistence import SqlClient
from jans.pycloudlib.persistence import PersistenceMapper
from jans.pycloudlib.persistence import doc_id_from_dn
from jans.pycloudlib.persistence import id_from_dn

from settings import LOGGING_CONFIG
from utils import get_config_api_scope_mapping

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("jans-config-api")

Entry = namedtuple("Entry", ["id", "attrs"])


def _transform_api_dynamic_config(conf):
    should_update = False

    # top-level config that need to be added (if missing)
    for missing_key, value in [
        ("userExclusionAttributes", ["userPassword"]),
        ("userMandatoryAttributes", [
            "mail",
            "displayName",
            "status",
            "userPassword",
            "givenName",
        ]),
        ("agamaConfiguration", {
            "mandatoryAttributes": [
                "qname",
                "source",
            ],
            "optionalAttributes": [
                "serialVersionUID",
                "enabled",
            ],
        }),
        ("auditLogConf", {
            "enabled": True,
            "headerAttributes": ["User-inum"],
        }),
        ("dataFormatConversionConf", {
            "enabled": True,
            "ignoreHttpMethod": [
                "@jakarta.ws.rs.GET()",
            ],
        }),
        ("customAttributeValidationEnabled", True),
        ("disableLoggerTimer", False),
        ("disableAuditLogger", False),
        ("assetMgtEnabled", True),
    ]:
        if missing_key not in conf:
            conf[missing_key] = value
            should_update = True

    if "plugins" not in conf:
        conf["plugins"] = []

    # current plugin names to lookup to
    plugins_names = tuple(plugin["name"] for plugin in conf["plugins"])

    supported_plugins = [
        {
            "name": "admin",
            "description": "admin-ui plugin",
            "className": "io.jans.ca.plugin.adminui.rest.ApiApplication"
        },
        {
            "name": "fido2",
            "description": "fido2 plugin",
            "className": "io.jans.configapi.plugin.fido2.rest.ApiApplication"
        },
        {
            "name": "scim",
            "description": "scim plugin",
            "className": "io.jans.configapi.plugin.scim.rest.ApiApplication"
        },
        {
            "name": "user-management",
            "description": "user-management plugin",
            "className": "io.jans.configapi.plugin.mgt.rest.ApiApplication"
        },
        {
            "name": "jans-link",
            "description": "jans-link plugin",
            "className": "io.jans.configapi.plugin.link.rest.ApiApplication"
        },
        {
            "name": "saml",
            "description": "saml plugin",
            "className": "io.jans.configapi.plugin.saml.rest.ApiApplication"
        },
        {
            "name": "kc-link",
            "description": "kc-link plugin",
            "className": "io.jans.configapi.plugin.kc.link.rest.ApiApplication"
        },
        {
            "name": "lock",
            "description": "lock plugin",
            "className": "io.jans.configapi.plugin.lock.rest.ApiApplication"
        }
    ]

    for supported_plugin in supported_plugins:
        if supported_plugin["name"] not in plugins_names:
            conf["plugins"].append(supported_plugin)
            should_update = True

    # userMandatoryAttributes.jansStatus is changed to userMandatoryAttributes.status
    if "jansStatus" in conf["userMandatoryAttributes"]:
        conf["userMandatoryAttributes"].remove("jansStatus")
        should_update = True

    if "status" not in conf["userMandatoryAttributes"]:
        conf["userMandatoryAttributes"].append("status")
        should_update = True

    # finalized conf and flag to determine update process
    return conf, should_update


class LDAPBackend:
    def __init__(self, manager):
        self.manager = manager
        self.client = LdapClient(manager)
        self.type = "ldap"

    def format_attrs(self, attrs):
        _attrs = {}
        for k, v in attrs.items():
            if len(v) < 2:
                v = v[0]
            _attrs[k] = v
        return _attrs

    def get_entry(self, key, filter_="", attrs=None, **kwargs):
        filter_ = filter_ or "(objectClass=*)"

        entry = self.client.get(key, filter_=filter_, attributes=attrs)
        if not entry:
            return None
        return Entry(entry.entry_dn, self.format_attrs(entry.entry_attributes_as_dict))

    def modify_entry(self, key, attrs=None, **kwargs):
        attrs = attrs or {}
        del_flag = kwargs.get("delete_attr", False)

        if del_flag:
            mod = self.client.MODIFY_DELETE
        else:
            mod = self.client.MODIFY_REPLACE

        for k, v in attrs.items():
            if not isinstance(v, list):
                v = [v]
            attrs[k] = [(mod, v)]
        return self.client.modify(key, attrs)

    def search_entries(self, key, filter_="", attrs=None, **kwargs):
        filter_ = filter_ or "(objectClass=*)"
        entries = self.client.search(key, filter_, attrs)

        return [
            Entry(entry.entry_dn, self.format_attrs(entry.entry_attributes_as_dict))
            for entry in entries
        ]


class SQLBackend:
    def __init__(self, manager):
        self.manager = manager
        self.client = SqlClient(manager)
        self.type = "sql"

    def get_entry(self, key, filter_="", attrs=None, **kwargs):
        table_name = kwargs.get("table_name")
        entry = self.client.get(table_name, key, attrs)

        if not entry:
            return None
        return Entry(key, entry)

    def modify_entry(self, key, attrs=None, **kwargs):
        attrs = attrs or {}
        table_name = kwargs.get("table_name")
        return self.client.update(table_name, key, attrs), ""

    def search_entries(self, key, filter_="", attrs=None, **kwargs):
        attrs = attrs or {}
        table_name = kwargs.get("table_name")
        return [
            Entry(entry["doc_id"], entry)
            for entry in self.client.search(table_name, attrs)
        ]


class CouchbaseBackend:
    def __init__(self, manager):
        self.manager = manager
        self.client = CouchbaseClient(manager)
        self.type = "couchbase"

    def get_entry(self, key, filter_="", attrs=None, **kwargs):
        bucket = kwargs.get("bucket")
        req = self.client.exec_query(
            f"SELECT META().id, {bucket}.* FROM {bucket} USE KEYS '{key}'"  # nosec: B608
        )
        if not req.ok:
            return None

        try:
            _attrs = req.json()["results"][0]
            id_ = _attrs.pop("id")
            entry = Entry(id_, _attrs)
        except IndexError:
            entry = None
        return entry

    def modify_entry(self, key, attrs=None, **kwargs):
        bucket = kwargs.get("bucket")
        del_flag = kwargs.get("delete_attr", False)
        attrs = attrs or {}

        if del_flag:
            kv = ",".join(attrs.keys())
            mod_kv = f"UNSET {kv}"
        else:
            kv = ",".join([
                "{}={}".format(k, json.dumps(v))
                for k, v in attrs.items()
            ])
            mod_kv = f"SET {kv}"

        query = f"UPDATE {bucket} USE KEYS '{key}' {mod_kv}"
        req = self.client.exec_query(query)

        if req.ok:
            resp = req.json()
            status = bool(resp["status"] == "success")
            message = resp["status"]
        else:
            status = False
            message = req.text or req.reason
        return status, message

    def search_entries(self, key, filter_="", attrs=None, **kwargs):
        bucket = kwargs.get("bucket")
        req = self.client.exec_query(
            f"SELECT META().id, {bucket}.* FROM {bucket} {filter_}"  # nosec: B608
        )
        if not req.ok:
            return []

        entries = []
        for item in req.json()["results"]:
            id_ = item.pop("id")
            entries.append(Entry(id_, item))
        return entries


class SpannerBackend:
    def __init__(self, manager):
        self.manager = manager
        self.client = SpannerClient(manager)
        self.type = "spanner"

    def get_entry(self, key, filter_="", attrs=None, **kwargs):
        table_name = kwargs.get("table_name")
        entry = self.client.get(table_name, key, attrs)

        if not entry:
            return None
        return Entry(key, entry)

    def modify_entry(self, key, attrs=None, **kwargs):
        attrs = attrs or {}
        table_name = kwargs.get("table_name")
        return self.client.update(table_name, key, attrs), ""

    def search_entries(self, key, filter_="", attrs=None, **kwargs):
        attrs = attrs or {}
        table_name = kwargs.get("table_name")
        return [
            Entry(entry["doc_id"], entry)
            for entry in self.client.search(table_name, attrs)
        ]


BACKEND_CLASSES = {
    "sql": SQLBackend,
    "couchbase": CouchbaseBackend,
    "spanner": SpannerBackend,
    "ldap": LDAPBackend,
}


class Upgrade:
    def __init__(self, manager):
        self.manager = manager

        mapper = PersistenceMapper()

        backend_cls = BACKEND_CLASSES[mapper.mapping["default"]]
        self.backend = backend_cls(manager)

    def invoke(self):
        logger.info("Running upgrade process (if required)")
        self.update_client_redirect_uri()
        self.update_api_dynamic_config()

        # add missing scopes into internal config-api client (if enabled)
        self.update_client_scopes()
        self.update_test_client_scopes()

        # creatorAttrs data type has been changed
        self.update_scope_creator_attrs()

    def update_client_redirect_uri(self):
        kwargs = {}
        jca_client_id = self.manager.config.get("jca_client_id")
        id_ = f"inum={jca_client_id},ou=clients,o=jans"

        if self.backend.type in ("sql", "spanner"):
            kwargs = {"table_name": "jansClnt"}
            id_ = doc_id_from_dn(id_)
        elif self.backend.type == "couchbase":
            kwargs = {"bucket": os.environ.get("CN_COUCHBASE_BUCKET_PREFIX", "jans")}
            id_ = id_from_dn(id_)

        entry = self.backend.get_entry(id_, **kwargs)

        if not entry:
            return

        should_update = False
        hostname = self.manager.config.get("hostname")

        if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
            if f"https://{hostname}/admin" not in entry.attrs["jansRedirectURI"]["v"]:
                entry.attrs["jansRedirectURI"]["v"].append(f"https://{hostname}/admin")
                should_update = True
        else:  # ldap, couchbase, and spanner
            if f"https://{hostname}/admin" not in entry.attrs["jansRedirectURI"]:
                entry.attrs["jansRedirectURI"].append(f"https://{hostname}/admin")
                should_update = True

        if should_update:
            self.backend.modify_entry(entry.id, entry.attrs, **kwargs)

    def update_api_dynamic_config(self):
        kwargs = {}
        id_ = "ou=jans-config-api,ou=configuration,o=jans"

        if self.backend.type in ("sql", "spanner"):
            kwargs = {"table_name": "jansAppConf"}
            id_ = doc_id_from_dn(id_)
        elif self.backend.type == "couchbase":
            kwargs = {"bucket": os.environ.get("CN_COUCHBASE_BUCKET_PREFIX", "jans")}
            id_ = id_from_dn(id_)

        entry = self.backend.get_entry(id_, **kwargs)

        if not entry:
            return

        if self.backend.type != "couchbase":
            with contextlib.suppress(json.decoder.JSONDecodeError):
                entry.attrs["jansConfDyn"] = json.loads(entry.attrs["jansConfDyn"])

        conf, should_update = _transform_api_dynamic_config(entry.attrs["jansConfDyn"])

        if should_update:
            if self.backend.type != "couchbase":
                entry.attrs["jansConfDyn"] = json.dumps(conf)

            entry.attrs["jansRevision"] += 1
            self.backend.modify_entry(entry.id, entry.attrs, **kwargs)

    def update_client_scopes(self):
        kwargs = {}
        client_id = self.manager.config.get("jca_client_id")
        id_ = f"inum={client_id},ou=clients,o=jans"

        if self.backend.type in ("sql", "spanner"):
            kwargs = {"table_name": "jansClnt"}
            id_ = doc_id_from_dn(id_)
        elif self.backend.type == "couchbase":
            kwargs = {"bucket": os.environ.get("CN_COUCHBASE_BUCKET_PREFIX", "jans")}
            id_ = id_from_dn(id_)

        entry = self.backend.get_entry(id_, **kwargs)

        if not entry:
            return

        if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
            client_scopes = entry.attrs["jansScope"]["v"]
        else:
            client_scopes = entry.attrs["jansScope"]

        if not isinstance(client_scopes, list):
            client_scopes = [client_scopes]

        # all potential new scopes for client
        scope_mapping = get_config_api_scope_mapping()
        new_client_scopes = [f"inum={inum},ou=scopes,o=jans" for inum in scope_mapping.keys()]

        # find missing scopes from the client
        diff = list(set(new_client_scopes).difference(client_scopes))

        if diff:
            if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
                entry.attrs["jansScope"]["v"] = client_scopes + diff
            else:
                entry.attrs["jansScope"] = client_scopes + diff
            self.backend.modify_entry(entry.id, entry.attrs, **kwargs)

    def update_test_client_scopes(self):
        test_client_id = self.manager.config.get("test_client_id")
        id_ = f"inum={test_client_id},ou=clients,o=jans"
        kwargs = {}

        # search_entries(self, key, filter_="", attrs=None, **kwargs)
        if self.backend.type in ("sql", "spanner"):
            id_ = doc_id_from_dn(id_)
            kwargs = {"table_name": "jansClnt"}
        elif self.backend.type == "couchbase":
            id_ = id_from_dn(id_)
            kwargs = {"bucket": os.environ.get("CN_COUCHBASE_BUCKET_PREFIX", "jans")}

        entry = self.backend.get_entry(id_, **kwargs)

        if not entry:
            return

        if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
            client_scopes = entry.attrs["jansScope"]["v"]
        else:
            client_scopes = entry.attrs["jansScope"]

        if not isinstance(client_scopes, list):
            client_scopes = [client_scopes]

        if self.backend.type in ("sql", "spanner"):
            scopes = [
                scope_entry.attrs["dn"]
                for scope_entry in self.backend.search_entries("", **{"table_name": "jansScope"})
            ]
        elif self.backend.type == "couchbase":
            bucket = os.environ.get("CN_COUCHBASE_BUCKET_PREFIX", "jans")
            scopes = [
                scope_entry.attrs["dn"]
                for scope_entry in self.backend.search_entries("", filter_="WHERE objectClass='jansScope'", **{"bucket": bucket})
            ]
        else:
            scopes = [
                scope_entry.id
                for scope_entry in self.backend.search_entries("ou=scopes,o=jans")
            ]

        # find missing scopes from the client
        diff = list(set(scopes).difference(client_scopes))
        if diff:
            if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
                entry.attrs["jansScope"]["v"] = client_scopes + diff
            else:
                entry.attrs["jansScope"] = client_scopes + diff
            self.backend.modify_entry(entry.id, entry.attrs, **kwargs)

    def update_scope_creator_attrs(self):
        kwargs = {}

        if self.backend.type != "sql":
            return

        kwargs.update({"table_name": "jansScope"})
        entries = self.backend.search_entries("", **kwargs)

        for entry in entries:
            if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
                creator_attrs = (entry.attrs.get("creatorAttrs") or {}).get("v") or []
            else:
                creator_attrs = entry.attrs.get("creatorAttrs") or []

            if not isinstance(creator_attrs, list):
                creator_attrs = [creator_attrs]

            new_creator_attrs = []

            # check the type of attr
            for _, attr in enumerate(creator_attrs):
                with contextlib.suppress(TypeError, json.decoder.JSONDecodeError):
                    # migrating from old data, i.e. `{"v": ["{}"]}`
                    attr = json.loads(attr)

                if isinstance(attr, str):
                    # migrating from old data, i.e. `{"v": ["\"{}\""]}`
                    attr = json.loads(attr.strip('"'))
                new_creator_attrs.append(attr)

            if new_creator_attrs != creator_attrs:
                if self.backend.type == "sql" and self.backend.client.dialect == "mysql":
                    entry.attrs["creatorAttrs"]["v"] = new_creator_attrs
                else:
                    entry.attrs["creatorAttrs"] = new_creator_attrs
                self.backend.modify_entry(entry.id, entry.attrs, **kwargs)


def main():
    manager = get_manager()

    with manager.lock.create_lock("config-api-upgrade"):
        upgrade = Upgrade(manager)
        upgrade.invoke()


if __name__ == "__main__":
    main()
