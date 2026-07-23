// Webhook receiver — a minimal axum server that receives and validates
// webhook POST requests from CubeAPI.
//
// Usage:
//   WEBHOOK_SECRET=your-secret cargo run
//
// Default: 127.0.0.1:9090. Override: PORT=8080 LISTEN=0.0.0.0

use axum::{routing::{get, post}, Router};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::env;

type HmacSha256 = Hmac<Sha256>;

#[tokio::main]
async fn main() {
    let secret = env::var("WEBHOOK_SECRET").unwrap_or_default();
    let secret = std::sync::Arc::new(secret);
    let listen = env::var("LISTEN").unwrap_or_else(|_| "127.0.0.1".to_string());
    let port = env::var("PORT").unwrap_or_else(|_| "9090".to_string());
    let addr = format!("{listen}:{port}");

    let has_secret = !secret.is_empty();

    let app = Router::new()
        .route("/webhook", post(handle_webhook))
        .route("/health", get(|| async { "ok" }))
        .with_state(secret);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    println!("webhook-receiver listening on http://{addr}");
    println!("  POST /webhook — receive webhook events");
    println!("  GET  /health  — health check");
    if has_secret {
        println!("  HMAC verification: enabled");
    }
    axum::serve(listener, app).await.unwrap();
}

async fn handle_webhook(
    axum::extract::State(secret): axum::extract::State<std::sync::Arc<String>>,
    headers: axum::http::HeaderMap,
    body: axum::body::Bytes,
) -> (axum::http::StatusCode, String) {
    // Verify HMAC signature FIRST (if configured)
    if !secret.is_empty() {
        if let Some(sig_header) = headers.get("X-Cube-Signature") {
            let sig = sig_header.to_str().unwrap_or("");
            let expected = sign_payload(&secret, &body);
            if sig != expected {
                println!("=== Webhook REJECTED (signature mismatch) ===");
                return (
                    axum::http::StatusCode::UNAUTHORIZED,
                    "signature mismatch".to_string(),
                );
            }
        } else {
            println!("=== Webhook REJECTED (missing X-Cube-Signature) ===");
            return (
                axum::http::StatusCode::UNAUTHORIZED,
                "X-Cube-Signature header missing".to_string(),
            );
        }
    }

    // Print only after validation passes
    println!("=== Webhook Received ===");
    for (name, value) in headers.iter() {
        if name.as_str().starts_with("x-cube-") || name.as_str() == "content-type" {
            println!("  {}: {}", name, value.to_str().unwrap_or("<non-utf8>"));
        }
    }

    let body_str = String::from_utf8_lossy(&body);
    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body_str) {
        println!("{}", serde_json::to_string_pretty(&parsed).unwrap());
    } else {
        println!("{}", body_str);
    }
    println!("========================\n");

    (axum::http::StatusCode::OK, "ok".to_string())
}

fn sign_payload(secret: &str, body: &[u8]) -> String {
    let mut mac = HmacSha256::new_from_slice(secret.as_bytes())
        .expect("HMAC can take key of any size");
    mac.update(body);
    format!("sha256={}", hex::encode(mac.finalize().into_bytes()))
}
