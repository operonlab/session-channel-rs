//! `GET /` — serves the session-channel dashboard HTML, with the local-key
//! injected so the page's JS can authenticate fetch + SSE when accessed
//! directly (no nginx-set cookie).
//!
//! 1:1 with Python `main.py::dashboard`:
//!
//!     html = template.read_text()
//!     inject = f'<meta name="local-key" content="{config.secret_key}">'
//!     html = html.replace("</head>", f"  {inject}\n</head>", 1)
//!
//! The template is embedded into the binary via `include_str!` so the
//! service has no runtime file dependency.

use axum::extract::State;
use axum::http::header;
use axum::response::IntoResponse;
use axum::response::Response;

use crate::service::AppState;

/// Embedded copy of the Python service's dashboard (templates/index.html).
const TEMPLATE_HTML: &str = include_str!("../../templates/index.html");

pub async fn dashboard(State(state): State<AppState>) -> Response {
    let inject = format!(
        r#"<meta name="local-key" content="{}">"#,
        html_escape(&state.cfg.secret_key)
    );
    // Python uses `replace("</head>", "  <meta>\n</head>", 1)` — emulate the
    // single replacement semantics with `replacen(..., 1)`.
    let html = TEMPLATE_HTML.replacen("</head>", &format!("  {inject}\n</head>"), 1);

    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        html,
    )
        .into_response()
}

/// Minimal HTML attribute-value escape — enough for a secret_key that should
/// never contain HTML metacharacters but we don't want to assume that.
fn html_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('"', "&quot;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}
