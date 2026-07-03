use std::collections::BTreeMap;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::str::FromStr;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InvalidPushNotificationConfigError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PinnedCallbackRequest {
    pub url: String,
    pub headers: BTreeMap<String, String>,
    pub sni_hostname: String,
    pub resolved_addresses: Vec<SocketAddr>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct CallbackEndpoint {
    scheme: String,
    host: String,
    port: Option<u16>,
}

impl CallbackEndpoint {
    fn parse(url: &str) -> Result<Self, InvalidPushNotificationConfigError> {
        let Some((scheme, rest)) = url.split_once("://") else {
            return Err(InvalidPushNotificationConfigError);
        };
        let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
        let authority = &rest[..authority_end];
        if authority.is_empty() {
            return Err(InvalidPushNotificationConfigError);
        }
        let host_port = authority
            .rsplit_once('@')
            .map_or(authority, |(_, host)| host);
        let (host, port) = parse_host_port(host_port)?;
        Ok(Self {
            scheme: scheme.to_ascii_lowercase(),
            host,
            port,
        })
    }

    pub(crate) fn host(&self) -> &str {
        &self.host
    }

    pub(crate) fn port_or_default(&self) -> Result<u16, InvalidPushNotificationConfigError> {
        self.port
            .map_or_else(|| default_callback_port(&self.scheme), Ok)
    }

    fn host_header(&self) -> String {
        let mut host_header = if self.host.contains(':') {
            format!("[{}]", self.host)
        } else {
            self.host.clone()
        };
        if let Some(port) = self.port {
            host_header = format!("{host_header}:{port}");
        }
        host_header
    }
}

pub fn validate_push_callback_url(url: &str) -> Result<String, InvalidPushNotificationConfigError> {
    let endpoint = parse_callback_endpoint(url)?;
    if !matches!(endpoint.scheme.as_str(), "http" | "https") {
        return Err(InvalidPushNotificationConfigError);
    }

    let host = endpoint.host.to_ascii_lowercase();
    if host == "localhost" || host.ends_with(".localhost") {
        return Err(InvalidPushNotificationConfigError);
    }

    if let Ok(address) = IpAddr::from_str(&host) {
        if is_private_or_local(address) {
            return Err(InvalidPushNotificationConfigError);
        }
    }

    Ok(url.to_owned())
}

pub fn pinned_callback_request(
    url: &str,
    address: &str,
    mut headers: BTreeMap<String, String>,
) -> Result<PinnedCallbackRequest, InvalidPushNotificationConfigError> {
    let endpoint = parse_callback_endpoint(url)?;
    let address = IpAddr::from_str(address).map_err(|_| InvalidPushNotificationConfigError)?;
    let port = endpoint.port_or_default()?;
    headers.insert("Host".to_owned(), endpoint.host_header());
    Ok(PinnedCallbackRequest {
        url: url.to_owned(),
        headers,
        sni_hostname: endpoint.host,
        resolved_addresses: vec![SocketAddr::new(address, port)],
    })
}

pub fn validate_resolved_callback_addresses<const N: usize>(
    addresses: [&str; N],
) -> Result<Vec<String>, InvalidPushNotificationConfigError> {
    validate_resolved_callback_address_iter(addresses)
}

pub(crate) fn parse_callback_endpoint(
    url: &str,
) -> Result<CallbackEndpoint, InvalidPushNotificationConfigError> {
    CallbackEndpoint::parse(url)
}

pub(crate) fn validate_resolved_callback_address_iter<I, S>(
    addresses: I,
) -> Result<Vec<String>, InvalidPushNotificationConfigError>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut verified = Vec::new();
    for address in addresses {
        let address =
            IpAddr::from_str(address.as_ref()).map_err(|_| InvalidPushNotificationConfigError)?;
        if is_private_or_local(address) {
            return Err(InvalidPushNotificationConfigError);
        }
        verified.push(address.to_string());
    }
    if verified.is_empty() {
        return Err(InvalidPushNotificationConfigError);
    }
    Ok(verified)
}

fn parse_host_port(
    host_port: &str,
) -> Result<(String, Option<u16>), InvalidPushNotificationConfigError> {
    if let Some(rest) = host_port.strip_prefix('[') {
        let Some((host, tail)) = rest.split_once(']') else {
            return Err(InvalidPushNotificationConfigError);
        };
        let port = tail.strip_prefix(':').map(parse_port).transpose()?;
        return Ok((host.to_owned(), port));
    }

    let (host, port) = match host_port.rsplit_once(':') {
        Some((host, port)) if !host.contains(':') && !port.contains(':') => {
            (host, Some(parse_port(port)?))
        }
        Some(_) if host_port.matches(':').count() > 1 => {
            return Err(InvalidPushNotificationConfigError);
        }
        _ => (host_port, None),
    };
    let host = host.trim();
    if host.is_empty() {
        return Err(InvalidPushNotificationConfigError);
    }
    Ok((host.to_owned(), port))
}

fn parse_port(port: &str) -> Result<u16, InvalidPushNotificationConfigError> {
    port.parse().map_err(|_| InvalidPushNotificationConfigError)
}

fn default_callback_port(scheme: &str) -> Result<u16, InvalidPushNotificationConfigError> {
    match scheme {
        "http" => Ok(80),
        "https" => Ok(443),
        _ => Err(InvalidPushNotificationConfigError),
    }
}

fn is_private_or_local(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_private_or_local_v4(address),
        IpAddr::V6(address) => is_private_or_local_v6(address),
    }
}

fn is_private_or_local_v4(address: Ipv4Addr) -> bool {
    address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_multicast()
        || address.is_broadcast()
        || address.is_documentation()
        || address.is_unspecified()
        || address.octets()[0] >= 240
}

fn is_private_or_local_v6(address: Ipv6Addr) -> bool {
    address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || is_unique_local_v6(address)
        || is_unicast_link_local_v6(address)
}

fn is_unique_local_v6(address: Ipv6Addr) -> bool {
    (address.segments()[0] & 0xfe00) == 0xfc00
}

fn is_unicast_link_local_v6(address: Ipv6Addr) -> bool {
    (address.segments()[0] & 0xffc0) == 0xfe80
}
