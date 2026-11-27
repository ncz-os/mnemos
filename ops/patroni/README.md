# HA configuration

Supply MNEMOS_DB_PASSWORD, MNEMOS_ADMIN_PASSWORD,
MNEMOS_REPLICATION_PASSWORD and MNEMOS_PATRONI_PASSWORD through a protected
operator environment. Each must contain at least 20 characters. The runtime
role has no role-creation, database-creation or superuser privileges. Use a
separate administration identity for schema migration. Existing installations
must rotate all template credentials and revoke elevated runtime privileges;
bootstrap settings only apply to new databases.

Set MNEMOS_NODE_IP to the node's management interface and MNEMOS_TLS_DIR to a
read-only certificate directory. Provision a dedicated cluster CA with separate
server, client and peer identities. Files required are ca.crt, server.crt,
server.key, peer.crt, peer.key, client.crt, client.key and client.pem (HAProxy's
combined client certificate and key). Server certificates must include the
configured management addresses as subject alternative names. Restrict private
key files to the service identity. Do not mount an operator home directory.

etcd requires client certificates for clients and peers. Patroni requires client
certificates and administration credentials for management operations. HAProxy
uses its client certificate for leader and replica health checks. Statistics
listen only on local loopback and require the management credential. Restrict management-network access to approved
nodes and administrators. Render credentials before starting Patroni; the
Compose command invokes render.py and stops when validation fails.

Validate certificate trust, administrative authorization, leader selection and
credential rotation in an isolated cluster before rollout. These checks require
running services and are not covered by configuration-only tests.
