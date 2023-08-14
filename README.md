# Better Jupyterhub SSH

This package provides a `jupyter_ssh_proxy` script that runs a reverse SSH proxy with jupyter hub integration.

## Features
 - Reverse SSH proxying
    - Feature complete pseudo-terminals
    - Support for local and remote tunneling
    - X-11 forwarding
    - SCP and SFTP support
    - Etc...
 - Concurrency via asyncio
 - Easy to customize for other use-cases
 - Interoperability with any SSH server/client following the official SSH specification

## Technical details
This proxy works by performing a classical Man-In-The-Middle "attack" on the SSH connection:
 1. The proxy poses as normal SSH-Endpoint and accepts client connections
 1. Any authentication attempts after the key exchange are verified using the jupyter hub REST-API.
 1. The proxy then connects to an internal SSH server (and starts it beforehand, if required).
 1. Any non-key-(re)-exchange and non-auth packets are forwarded to and from the internal SSH server/the client.

## Even more technical details
The [asyncssh](https://pypi.org/project/asyncssh/) library is used as base. It uses handler callbacks to process received packets, associated with the connection or a specific channel inside. Connection-associated handlers are directly overwritten with forwarding handlers, and packets for channel-associated handlers are delegated to a dummy channel object that also forwards the packets.

Special care must be taken in multiple cases:
   - SSH_MSG_UNIMPLEMENTED packets have to have the contained sequence numbers mapped to the respective new connection.
   - SSH_MSG_SERVICE_REQUEST and SSH_MSG_SERVICE_ACCEPT packets can contain requests for the *ssh-userauth* service, which need to be processed by the proxy instead of being forwarded.
   - SSH_MSG_DISCONNECT packets must be forwarded and also processed by the proxy, to close connections at all sides.
   - SSH_MSG_EXT_INFO packets are (currently) ignored. This due to the fact that extensions are, by definition, optional and with varying support/usage, thus making it hard to implement appropriate behaviour for long term use.

## Installation
### From source
 ```
 git clone <URL>
 cd better_jupyterhub_ssh
 pip install .
 jupyter_ssh_proxy -p [PORT] -k [HOST_KEY_DIR] <JUPYTER_HUB_URL>
 ```

## Disadvantages
 - Since SSH does not support redirects, the connections only work as long as the proxy is running
 - Even though the connections between the proxy and the server/client are safe and encrypted, the proxy itself has (in theory) access to all data, unencrypted