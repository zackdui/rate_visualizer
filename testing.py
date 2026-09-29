import ijson, re
from collections import defaultdict
from bcbstx_mrf import open_local, download, TOC_URL

nets = defaultdict(set)
with open_local(download(TOC_URL)) as fh:
    for f in ijson.items(fh, "reporting_structure.item.in_network_files.item"):
        name = re.sub(r"\s*\d+\s+of\s+\d+\s*$", "", f["description"] or "").strip()
        nets[name].add(f["location"].split("?")[0])

for name, urls in sorted(nets.items(), key=lambda x: -len(x[1])):
    print(f"{len(urls):4d} files  {name}")