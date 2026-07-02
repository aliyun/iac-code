#[derive(Clone, Debug)]
pub(super) struct RawAuthAliyunOAuthSite {
    pub(super) site_type: &'static str,
    pub(super) display_name: &'static str,
    pub(super) client_id: &'static str,
    pub(super) signin_base_url: &'static str,
    pub(super) oauth_base_url: &'static str,
}

#[derive(Clone, Debug)]
pub(super) struct RawAuthAliyunOAuthToken {
    pub(super) access_token: String,
    pub(super) refresh_token: String,
    pub(super) access_token_expire: i64,
    pub(super) refresh_token_expire: i64,
}

#[derive(Clone, Debug)]
pub(super) struct RawAuthAliyunOAuthSts {
    pub(super) access_key_id: String,
    pub(super) access_key_secret: String,
    pub(super) sts_token: String,
    pub(super) sts_expiration: i64,
}

#[derive(Clone, Debug)]
pub(super) struct RawAuthAliyunOAuthResult {
    pub(super) token: RawAuthAliyunOAuthToken,
    pub(super) sts: RawAuthAliyunOAuthSts,
}

pub(super) fn raw_auth_aliyun_oauth_sites() -> Vec<RawAuthAliyunOAuthSite> {
    vec![
        RawAuthAliyunOAuthSite {
            site_type: "CN",
            display_name: "China",
            client_id: "4038181954557748008",
            signin_base_url: "https://signin.aliyun.com",
            oauth_base_url: "https://oauth.aliyun.com",
        },
        RawAuthAliyunOAuthSite {
            site_type: "INTL",
            display_name: "International",
            client_id: "4103531455503354461",
            signin_base_url: "https://signin.alibabacloud.com",
            oauth_base_url: "https://oauth.alibabacloud.com",
        },
    ]
}
