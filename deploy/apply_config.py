"""Apply non-secret config vars to a Heroku app. PATCH semantics: only listed keys are set, none are unset."""
import json, os, re, sys, urllib.request

SECRET = re.compile(r"(TOKEN(?!S$)|SECRET|PASSWORD|PRIVATE_KEY|API_KEY|(^|_)KEY$)", re.I)


def parse(path):
    out = {}
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k = k.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", k):
            sys.exit(f"{path}:{n}: invalid line")
        if SECRET.search(k):
            sys.exit(f"{path}:{n}: {k} looks like a secret; secrets must not be in this file")
        out[k] = v
    return out


def main(path, app):
    key = os.environ.get("HEROKU_API_KEY")
    if not key:
        sys.exit("HEROKU_API_KEY is not set")
    cfg = parse(path)
    if not cfg:
        sys.exit("no config vars found")
    req = urllib.request.Request(
        f"https://api.heroku.com/apps/{app}/config-vars", method="PATCH", data=json.dumps(cfg).encode(),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/vnd.heroku+json; version=3",
                 "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60).read()
    print("applied keys:", ", ".join(sorted(cfg)))


if __name__ == "__main__":
    main(*sys.argv[1:3])
