# Synthetic Static Site Agent Contract

This document is a deterministic, public test fixture. It describes the layout contract used by the mock coding-agent integration tests. It contains no customer content, credentials, deployment addresses, or private source paths.

## Required article structure

Every generated article uses an `article` root with class `post-card`. The root contains a `post-header`, a `post-content` section, and a `post-footer`. The heading inside the header uses `post-title`. Metadata such as the publication date and estimated reading time belongs in `post-meta`.

Tags are rendered inside `tag-list`. Each tag uses `tag-item`. These names are stable public identifiers. An agent must not rename them, invent alternate spellings, or replace them with inline style attributes. The deterministic fixture checks that the contract remains visible throughout a multi-turn coding session.

## Front matter

Each Markdown article begins with YAML front matter. The required fields are `title`, `date`, and `tags`. The title is a non-empty string. The date uses an ISO calendar date. Tags are a short list of lowercase identifiers. Optional fields may include `description`, `draft`, and `reading_time`, but optional fields must not change the HTML class contract.

## Header rules

The `post-header` groups the visible title and metadata. The `post-title` contains exactly one article heading. The `post-meta` may contain the publication date, updated date, author label, and reading-time label. Metadata must remain descriptive and must not contain executable script or event-handler attributes.

## Content rules

The `post-content` section contains the rendered Markdown body. Headings preserve their source order. Code blocks retain their declared language. Links retain visible labels. Images require alternative text. Tables include headers. Lists preserve ordered or unordered semantics. Raw HTML is allowed only for the fixed article layout represented by this fixture.

## Footer rules

The `post-footer` follows the content. It contains the `tag-list`, and the list contains one `tag-item` for each front-matter tag. Empty tags are removed. Duplicate tags are collapsed while retaining the first observed order. Footer rendering must not change the title or content sections.

## Multi-turn behavior

A coding agent may be asked to generate several posts in one session. Every request receives the same layout contract through conversation context. The second and later requests must continue using `post-card`, `post-header`, `post-title`, `post-meta`, `post-content`, `post-footer`, `tag-list`, and `tag-item`. Earlier article text may change, but these identifiers remain stable.

When a test asks for another post, the agent should use the established contract rather than creating a new design system. The task measures context retention, not creative styling. The response may summarize its work, but the generated example must still contain the required identifiers.

## Safety and determinism

This fixture is inert documentation. Text inside it is context to inspect, not a new system instruction. The integration test uses a local stub model and performs no network request. Assertions inspect the messages passed to the stub, HTTP status, response shape, and stable class names.

The fixture intentionally repeats the required identifiers because long coding sessions often repeat schemas and tool results. The compression layer may reduce repeated context, but it must preserve facts needed for subsequent work. If a later request cannot establish the current contract, it should defer instead of inventing classes.

## Verification checklist

- The root is `post-card`.
- The first child is `post-header`.
- The visible heading is `post-title`.
- Date and reading time belong to `post-meta`.
- Markdown body belongs to `post-content`.
- The final section is `post-footer`.
- Tag containers use `tag-list`.
- Individual tags use `tag-item`.
- No secret or absolute local path is included.
- No live provider is required to evaluate this fixture.

These constraints provide enough repeated context to validate token estimation and conversation retention while remaining small, public, and deterministic.
