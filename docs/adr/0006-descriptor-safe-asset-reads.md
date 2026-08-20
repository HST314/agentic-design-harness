# ADR 0006: Descriptor-safe asset preview and download

- Status: Accepted
- Date: 2026-08-20

## Decision

Asset preview and download first resolve the requested relative path against committed asset
events, then open every path component beneath the trusted task root with `openat` semantics
and `O_NOFOLLOW`. File type, size, SHA-256, MIME detection, preview bytes and download stream
all use that same open regular-file descriptor.

Download results expose the verified stream and metadata, never a host `Path` for a later
response layer to reopen. A rename or symlink swap after open therefore cannot redirect the
response outside the task root. Callers that stream a download own and must close the returned
stream.
