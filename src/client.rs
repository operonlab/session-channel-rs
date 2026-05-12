//! Thin HTTP client wrapper. The skeleton uses reqwest blocking so the CLI
//! stays a single-shot process without spinning up its own tokio runtime.

use std::time::Duration;

use anyhow::{anyhow, Result};
use reqwest::blocking::{Client, RequestBuilder};
use serde::de::DeserializeOwned;
use serde::Serialize;

use crate::config::Config;

const TIMEOUT: Duration = Duration::from_secs(10);
const HEADER_LOCAL_KEY: &str = "x-local-key";

pub struct ApiClient {
    cfg: Config,
    http: Client,
}

impl ApiClient {
    pub fn new() -> Result<Self> {
        let http = Client::builder()
            .timeout(TIMEOUT)
            .build()
            .map_err(|e| anyhow!("failed to build HTTP client: {e}"))?;
        Ok(Self {
            cfg: Config::from_env(),
            http,
        })
    }

    fn auth(&self, rb: RequestBuilder) -> RequestBuilder {
        rb.header(HEADER_LOCAL_KEY, &self.cfg.local_key)
    }

    /// POST `path` with `body`. Returns parsed JSON or the raw error text + status.
    pub fn post_json<B, R>(&self, path: &str, body: &B) -> Result<R>
    where
        B: Serialize + ?Sized,
        R: DeserializeOwned,
    {
        let url = format!("{}{}", self.cfg.base_url, path);
        let resp = self.auth(self.http.post(&url).json(body)).send()?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().unwrap_or_default();
            return Err(anyhow!("{status}: {body}"));
        }
        let parsed = resp.json::<R>()?;
        Ok(parsed)
    }

    /// GET `path` (optional query) and decode JSON.
    pub fn get_json<R>(&self, path: &str, query: &[(&str, &str)]) -> Result<R>
    where
        R: DeserializeOwned,
    {
        let url = format!("{}{}", self.cfg.base_url, path);
        let resp = self.auth(self.http.get(&url).query(query)).send()?;
        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().unwrap_or_default();
            return Err(anyhow!("{status}: {body}"));
        }
        Ok(resp.json::<R>()?)
    }
}
