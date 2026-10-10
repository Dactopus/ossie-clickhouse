"""Build a model with two unrelated facts from the entities of dactopus-data-models.

commerce (orders, order lines, refunds) and web_analytics (GA4 sessions,
purchases, events) share no relationship, so a question over both has no
stitching dimension (E3013) or no path (E_NO_PATH). Each side reads its own
database; the GA4 metrics revenue and average_order_value are renamed
web_revenue and web_average_order_value, since commerce has metrics of those
names.

Usage: python two_facts.py ENTITIES_DIR COMMERCE_DB WEB_DB OUT.yaml
"""

import sys

import yaml

RENAMED = {"revenue": "web_revenue", "average_order_value": "web_average_order_value"}


def main():
    entities, commerce_db, web_db, out = sys.argv[1:5]
    with open(f"{entities}/commerce.yaml") as f:
        commerce = yaml.safe_load(f)
    with open(f"{entities}/web_analytics.yaml") as f:
        web = yaml.safe_load(f)
    for ds in commerce["datasets"]:
        ds["source"] = f"{commerce_db}.{ds['source']}"
    for ds in web["datasets"]:
        ds["source"] = f"{web_db}.{ds['source']}"
    for m in web["metrics"]:
        m["name"] = RENAMED.get(m["name"], m["name"])
    model = {
        "version": commerce["version"],
        "name": "commerce_and_web",
        "description": "Two unrelated facts, commerce orders and GA4 sessions, for Layer 3 checks.",
        "datasets": commerce["datasets"] + web["datasets"],
        "relationships": commerce["relationships"] + web["relationships"],
        "metrics": commerce["metrics"] + web["metrics"],
    }
    with open(out, "w") as f:
        yaml.safe_dump(model, f, sort_keys=False, allow_unicode=True)


if __name__ == "__main__":
    main()
