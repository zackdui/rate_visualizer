"""Laptop-side steps that turn the pipeline's rates.duckdb into the website's site.duckdb and publish it.

build_site: rates.duckdb (+ entity tags + map data) -> site.duckdb
publish:    site.duckdb -> Cloudflare R2 (+ latest.json) -> Render deploy hook
"""
