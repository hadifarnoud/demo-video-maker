#!/usr/bin/env bash
# Demo-video pipeline. Run from a project's demo-videos folder (it holds project.json and one folder per video).
#   box.sh init <project>           write a starter project.json here (then edit it)
#   box.sh new <video>              start a video folder from the template
#   box.sh sync                     copy the engine and video sources to the box, build the image, update the app checkout
#   box.sh app                      build + start the app container (idempotent); no-op when project.json has no app
#   box.sh reseed                   re-run the demo seed in the running app container
#   box.sh run <video> <step> ...   step: voice [scene] [--force] [--music] [--recheck] | record | edit [--preview|--plan] | gate [--no-stt] | frames [--n 20] | text | gif [--start S --dur D --width W --fps F --subs]
#   box.sh fetch <video>            copy out/, clip logs and voice takes to the media folder
#   box.sh review <video> [--port N]  local page: change what the voice reads, hear it, keep the take you approve
#   box.sh drift <video> [--shots]  without filming, run every step and label against the current app: does the video still match?
#                                   --shots also saves one PNG per target with its box drawn (drift-shots/)
#   box.sh apply <video>            ship the kept takes: re-record the changed scenes, edit, gate, fetch
#   box.sh rerecord <video> [passes]  re-record scenes with a late cue or a low capture rate
#   box.sh all <video>              sync, app, voice, record, edit, gate, fetch
# Media lives outside the repo: ${DEMO_MEDIA:-~/DemoVideos}/<project>/<video>. SCENES=2,5 limits a record step.
# ELEVENLABS_API_KEY is read from this shell's env and sent over ssh stdin only.
set -euo pipefail
ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
HERE="$PWD"
IMG=dvm-engine

# one value from project.json ('' when absent)
pj() { python3 -c 'import json,sys
d=json.load(open("project.json"))
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
print("" if d is None else d)' "$1"; }

case "${1:-}" in
  init)
    [ -e project.json ] && { echo "project.json already exists" >&2; exit 1; }
    sed "s/my-project/${2:?project name}/" "$ENGINE/_template/project.json" > project.json; echo "wrote project.json: edit it"; exit 0 ;;
  new)
    [ -e "${2:?video}" ] && { echo "$2 already exists" >&2; exit 1; }
    mkdir "$2"; cp "$ENGINE"/_template/{narration.txt,scenes.spec.ts,storyboard.md,video.json} "$2/"; echo "started $2"; exit 0 ;;
  ""|-h|--help) sed -n '2,17p' "$0"; exit 2 ;;
esac
[ -f project.json ] || { echo "no project.json here: run this from the project's demo-videos folder (box.sh init <project>)" >&2; exit 1; }
PROJECT="$(pj project)"; BOX="$(pj box)"; APPC="$(pj app.container)"
MEDIA="${DEMO_MEDIA:-$HOME/DemoVideos}/$PROJECT"
REMOTE="work/dvm/$PROJECT/videos"
retry() { local i; for i in 1 2 3 4; do "$@" && return 0; sleep 10; done; return 1; }  # ssh to the box drops under load

sync_all() {
  ssh "$BOX" "mkdir -p work/dvm/engine $REMOTE"
  retry rsync -a --delete --exclude node_modules --exclude .venv --exclude __pycache__ "$ENGINE/" "$BOX:work/dvm/engine/"
  local d
  for d in */; do
    [ -f "$d/video.json" ] || continue
    case "$d" in _*) continue ;; esac
    retry rsync -a --delete --exclude clips --exclude audio --exclude out --exclude state --exclude work "$d" "$BOX:$REMOTE/$d"
    # kept takes go with the sources that name them, or the box would see a stale take and voice it again
    [ -d "$MEDIA/$d/audio" ] && retry rsync -a --update --exclude music.mp3 "$MEDIA/$d/audio/" "$BOX:$REMOTE/${d}audio/"
  done
  ssh -o ServerAliveInterval=20 "$BOX" "docker build -q -t $IMG work/dvm/engine"
  local remote; remote="$(pj git.remote)"
  if [ -n "$remote" ]; then
    local branch bare checkout; branch="$(pj git.branch)"; bare="$(pj git.bare)"; checkout="$(pj app.checkout)"
    git -C "$HERE" push -q -f "$remote" "HEAD:refs/heads/$branch"
    ssh -o ServerAliveInterval=20 "$BOX" "set -e; mkdir -p work; [ -d $checkout/.git ] || git clone -q -b $branch $bare $checkout
      cd $checkout && git fetch -q origin $branch && git checkout -q -f $branch && git reset -q --hard origin/$branch && git log --oneline -1"
  fi
}

app() {
  [ -n "$APPC" ] || { echo "no app in project.json: nothing to start"; return 0; }
  local script
  script="$(python3 - <<'PY'
import json, shlex
a = json.load(open("project.json"))["app"]
q = shlex.quote
env = " ".join(f"-e {k}={q(str(v))}" for k, v in a.get("env", {}).items())
rand = " ".join(f"-e {k}=$(head -c 30 /dev/urandom | base64 | tr -dc a-zA-Z0-9 | head -c 40)" for k in a.get("random_env", []))
vol = f"-v {a['volume']}:{a['data_dir']}" if a.get("volume") else ""
rmvol = f"docker volume rm -f {a['volume']} >/dev/null 2>&1 || true" if a.get("volume") else ""
stamp = " ".join(f"HEAD:{p}" for p in a.get("stamp_paths", ["."]))
print(f"""set -e; cd {a['checkout']}
docker network inspect {a['network']} >/dev/null 2>&1 || docker network create {a['network']} >/dev/null
STAMP=$(git rev-parse {stamp} | tr '\\n' ' ')
if [ "$(cat .app-stamp 2>/dev/null)" = "$STAMP" ] && docker ps -q -f name=^{a['container']}$ | grep -q .; then echo 'app up to date'; else
docker rm -f {a['container']} >/dev/null 2>&1 || true
{rmvol}
docker run -d --name {a['container']} --network {a['network']} -v $PWD:/app {vol} -w /app {env} {rand} {a['image']} bash -c {q(a['start'])} >/dev/null
echo "$STAMP" > .app-stamp
fi
for i in $(seq 1 120); do
  docker exec {a['container']} node -e "fetch('http://localhost:{a['port']}/').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))" 2>/dev/null && {{ echo 'app ready'; exit 0; }}
  docker ps -q -f name=^{a['container']}$ | grep -q . || {{ docker logs --tail 40 {a['container']}; echo 'app container died'; exit 1; }}
  sleep 5
done
docker logs --tail 40 {a['container']}; exit 1""")
PY
)"
  retry ssh -o ServerAliveInterval=20 "$BOX" "$script"  # idempotent, and waits: a dropped session must not mean "ready"
}

# step runner: the key goes over stdin, never argv
runstep() {
  local video="$1"; shift
  local cmd extra="" v netarg="" base
  if [ -n "$APPC" ]; then netarg="--network $(pj app.network)"; base="http://$APPC:$(pj app.port)"; else base="$(pj base_url)"; fi
  case "$1" in
    voice)  shift; cmd="python lib/voice.py $video $*" ;;
    # record shares the app container's network namespace: an app that sets Secure cookies only works on https or
    # http://localhost in Chromium, never on http://<container>:<port>
    record) shift; cmd="npx playwright test $*"
      # a DOM edit must go through s.rewrite / s.mask (logged); a raw one fails here, before a minute of filming, and in the gate
      if ! python3 "$ENGINE/lib/specscan.py" "$HERE/$video" >&2; then
        if [ "${DEMO_DRY:-}" = 1 ]; then echo "[drift] the spec has raw DOM edits (above); the gate would fail them" >&2
        else echo "record refused: the spec has raw DOM edits" >&2; return 1; fi
      fi
      if [ -n "$APPC" ]; then netarg="--network container:$APPC"; base="http://localhost:$(pj app.port)"; fi
      extra="$(python3 - "$video" <<'PY'
import json, shlex, sys
for k, v in json.load(open(sys.argv[1] + "/video.json")).get("record_env", {}).items():
    print(f" -e {k}={shlex.quote(str(v))}", end="")
PY
)" ;;
    edit)   shift; cmd="python lib/edit.py $video $*" ;;
    gate)   shift; cmd="python lib/gate.py $video $*" ;;
    gif)    shift; cmd="python lib/gif.py $video $*" ;;
    text)   shift; cmd="python lib/textsize.py $video" ;;
    frames) shift; cmd="python lib/frames.py $video $*" ;;
    *) echo "unknown step $1" >&2; exit 2 ;;
  esac
  for v in DEMO_DRY DEMO_SHOTS SCENE_TIMEOUT_MS DEMO_START_LANG DEMO_ENTRY_URL DEMO_LOGIN_URL DEMO_EMAIL DEMO_PASSWORD DEMO_AFTER_URL; do
    [ -n "${!v:-}" ] && extra+=" -e $v=$(printf '%q' "${!v}")"
  done
  # Detached and followed with `docker logs -f`: the box drops ssh sessions under load, and a dropped session used to
  # kill a 10-minute edit.
  local name="dvm-run-$video-$$" since
  printf '%s\n' "${ELEVENLABS_API_KEY:-}" | ssh -o ServerAliveInterval=20 "$BOX" "set -e; read -r ELEVENLABS_API_KEY; export ELEVENLABS_API_KEY
    mkdir -p $REMOTE/$video
    docker run -d --name $name --cpu-shares 262144 $netarg -e ELEVENLABS_API_KEY -e VIDEO=$video -e BASE_URL=$base -e SCENES=\"\${SCENES:-${SCENES:-}}\"$extra \
      -v \$HOME/$REMOTE/$video:/work/$video $IMG bash -c '$cmd'" >/dev/null
  since=0
  for _ in $(seq 1 400); do
    ssh -o ServerAliveInterval=20 -o ConnectTimeout=30 "$BOX" "docker logs -f --since $since $name 2>&1" || true
    since=$(( $(date +%s) - 20 ))  # a re-attach may repeat a few lines
    if [ "$(ssh -o ConnectTimeout=30 "$BOX" "docker inspect -f '{{.State.Running}}' $name" 2>/dev/null || echo retry)" = false ]; then break; fi
    sleep 5
  done
  local rc
  rc=$(ssh -o ConnectTimeout=30 "$BOX" "docker inspect -f '{{.State.ExitCode}}' $name; docker rm $name >/dev/null" | head -1)
  return "${rc:-1}"
}

fetch() {
  local v="$1"; mkdir -p "$MEDIA/$v/out" "$MEDIA/$v/clips" "$MEDIA/$v/audio"
  retry ssh "$BOX" "mkdir -p $REMOTE/$v/out $REMOTE/$v/clips $REMOTE/$v/audio"
  retry rsync -a "$BOX:$REMOTE/$v/out/" "$MEDIA/$v/out/"
  retry rsync -a --include='*.json' --include='fail-*.png' --exclude='*' "$BOX:$REMOTE/$v/clips/" "$MEDIA/$v/clips/"  # fail-NN.png: the screen of a failed take
  # --update: a take kept in the review page is newer than the box's copy and must survive
  retry rsync -a --update --exclude music.mp3 "$BOX:$REMOTE/$v/audio/" "$MEDIA/$v/audio/"
  echo "fetched to $MEDIA/$v"
}

rerecord() {
  local v="$1" passes="${2:-4}" i list
  for i in $(seq 1 "$passes"); do
    fetch "$v" >/dev/null 2>&1 || true
    list=$(python3 - "$MEDIA/$v" "$HERE/$v" <<'PY'
import glob, json, sys
g = json.load(open(sys.argv[2] + "/video.json")).get("gate", {})
bad = []
for f in sorted(glob.glob(sys.argv[1] + "/clips/scene-*.json")):
    l = json.load(open(f))
    mn = g.get("capture_allow", {}).get(str(l["scene"]), {}).get("min", g.get("min_capture_fps", 20))
    if l["late"] or l["capture"]["avg_fps"] < mn:
        bad.append(str(l["scene"]))
print(",".join(bad))
PY
)
    [ -z "$list" ] && { echo "rerecord: every clip is on time and above the capture floor"; return 0; }
    echo "rerecord pass $i: scenes $list"
    SCENES="$list" runstep "$v" record || true
  done
  fetch "$v" >/dev/null 2>&1 || true
}

# Kept takes -> final video. Each video.json record_groups entry records in its own fresh sandbox (a scene that must not
# see another scene's writes); the other changed scenes record together.
apply() {
  local v="$1" batches b
  [ -f "$MEDIA/$v/review.json" ] || { echo "nothing to apply: no review.json in $MEDIA/$v" >&2; return 1; }
  batches="$(python3 - "$MEDIA/$v/review.json" "$v/video.json" <<'PY'
import json, sys
ch = set(json.load(open(sys.argv[1]))["changed"])
out = []
for g in json.load(open(sys.argv[2])).get("record_groups", []):
    g = sorted(ch & set(g))
    ch -= set(g)
    if g: out.append(g)
if ch: out.insert(0, sorted(ch))
print("\n".join(",".join(map(str, g)) for g in out))
PY
)"
  [ -n "$batches" ] || { echo "nothing to apply: no scene changed"; return 0; }
  sync_all; app
  runstep "$v" voice --music  # skips every take that is still current; a longer script may outgrow the bed
  for b in $batches; do echo "record scenes $b"; SCENES="$b" runstep "$v" record; done
  runstep "$v" edit
  local rc=0; runstep "$v" gate || rc=$?
  fetch "$v"
  if [ "$rc" = 0 ]; then
    # only what shipped: a scene kept in the open page during this run stays queued
    python3 - "$MEDIA/$v/review.json" $batches <<'PY'
import json, sys
p, done = sys.argv[1], {int(x) for b in sys.argv[2:] for x in b.split(",")}
d = json.load(open(p))
d["changed"] = [n for n in d["changed"] if n not in done]
json.dump(d, open(p, "w"))
PY
  else echo "gate failed: the scenes stay queued" >&2; fi
  return "$rc"
}

SCENES="${SCENES:-}"
case "$1" in
  sync) sync_all ;;
  app) app ;;
  reseed) ssh -o ServerAliveInterval=20 "$BOX" "docker exec $APPC sh -c $(printf '%q' "$(pj app.reseed)")" ;;
  run) shift; v="$1"; shift; runstep "$v" "$@" ;;
  fetch) fetch "$2" ;;
  review) v="$2"; shift 2; fetch "$v" >/dev/null 2>&1 || true; exec python3 "$ENGINE/lib/review.py" "$HERE/$v" "$MEDIA/$v" "$@" ;;
  apply) apply "$2" ;;
  drift) v="$2"; shift 2
    # --shots: one PNG per click/hover/look/type/named label with its box drawn, in $MEDIA/<video>/drift-shots/ (nothing is filmed or paid for)
    if [ "${1:-}" = --shots ]; then export DEMO_SHOTS=1; fi
    sync_all; app; rc=0
    DEMO_DRY=1 SCENE_TIMEOUT_MS="${SCENE_TIMEOUT_MS:-60000}" runstep "$v" record || rc=$?
    if [ "${DEMO_SHOTS:-}" = 1 ]; then
      mkdir -p "$MEDIA/$v/drift-shots"
      retry rsync -a --delete "$BOX:$REMOTE/$v/drift-shots/" "$MEDIA/$v/drift-shots/" && echo "shots in $MEDIA/$v/drift-shots"
    fi
    exit "$rc" ;;
  rerecord) rerecord "$2" "${3:-4}" ;;
  all) v="$2"; sync_all; app; runstep "$v" voice; runstep "$v" record; runstep "$v" edit; runstep "$v" gate || true; fetch "$v" ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
