//! The download decision layer: the RFC 7232 / RFC 7233 semantic machine.
//!
//! Answers how Range and conditional requests are decided for a download
//! (206 / 304 / 412 / 416). The five submodules are moved verbatim from
//! `rest/{preconditions,ranges,encoding,httpdate,content_disposition}.rs`;
//! their semantics track upstream `http.ServeContent`, and this file only
//! aggregates them.

pub mod content_disposition;
pub mod encoding;
pub mod httpdate;
pub mod preconditions;
pub mod ranges;
