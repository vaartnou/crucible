import {loadFiles, pollManifest} from './feed.js';
import {createUI} from './ui.js';
import {createSimulationControls} from './controls.js';

let groups = [];
let stopFeed = null;
let playback = null;
let generation = 0;
let follow = false;
const state = {
  source: '', status: 'Ready', statusDetail: '', connected: false, playing: false,
  groupId: '', groups: [], runs: [], primary: '', index: 0, maxIndex: 0,
};

function draw() {
  viewer.render(state);
}

function stopClock() {
  clearInterval(playback);
  playback = null;
  state.playing = false;
  follow = false;
}

function reset(source) {
  generation++;
  stopFeed?.();
  stopFeed = null;
  stopClock();
  groups = [];
  Object.assign(state, {source, status: 'Loading', statusDetail: '', connected: false,
    groups: [], groupId: '', runs: [], primary: '', index: 0, maxIndex: 0});
  draw();
  return generation;
}

function showGroup(id, initial = false) {
  const group = groups.find(item => item.id === id) ?? groups[0];
  if (!group) return;
  const buyer = group.runs.find(run => run.strategy === group.primary);
  state.groupId = group.id;
  state.runs = group.runs;
  state.primary = group.primary;
  state.maxIndex = buyer?.weeks.at(-1)?.week ?? 0;
  state.index = follow ? state.maxIndex : Math.min(state.index, state.maxIndex);
  if (initial && !state.connected) state.index = Math.min(1, state.maxIndex);
  if (initial && state.source === 'Scripted replay') {
    // Open on a real control change after enough history has accumulated to compare curves.
    const decision = buyer?.weeks.find(event => event.week >= 18 &&
      event.observation?.controls?.expedite_fraction !== event.applied_controls?.expedite_fraction);
    state.index = decision?.week ?? Math.min(8, state.maxIndex);
  }
  state.statusDetail = group.versionMismatch ? 'Different simulator or scorer versions were separated.' : '';
  state.status = buyer?.final ? (buyer.final.completed ? 'Run complete' : 'Run stopped') :
    (state.connected ? 'Watching feed' : 'Recorded feed');
  if (state.connected && buyer?.final && follow) stopClock();
  draw();
}

function receive(next) {
  groups = next;
  state.groups = groups.map(({id,label}) => ({id,label}));
  if (!groups.length) {
    state.status = 'Waiting for spectator events';
    draw();
    return;
  }
  showGroup(state.groupId, !state.groupId);
}

function fail(error) {
  state.status = 'Feed unavailable';
  state.statusDetail = error.message || String(error);
  draw();
}

function connect(url, demo = false) {
  const token = reset(demo ? 'Scripted replay' : 'Live feed');
  state.connected = !demo;
  follow = !demo;
  state.playing = follow;
  stopFeed = pollManifest(url, next => {
    if (generation !== token) return;
    receive(next);
    if (demo && next.some(group => group.runs.length === 5 && group.runs.every(run => run.final))) {
      stopFeed?.();
      stopFeed = null;
    }
  }, error => { if (generation === token) fail(error); });
}

function play() {
  stopClock();
  if (state.connected) {
    follow = true;
    state.index = state.maxIndex;
  } else {
    if (!state.maxIndex) return;
    if (state.index >= state.maxIndex) state.index = 0;
    playback = setInterval(() => {
      state.index = Math.min(state.index + 1, state.maxIndex);
      if (state.index === state.maxIndex) stopClock();
      draw();
    }, 650);
  }
  state.playing = true;
  draw();
}

const viewer = createUI({
  onDemo: () => connect('./demo/spectator-manifest.json', true),
  onConnect: url => connect(url),
  onDisconnect: () => {
    generation++;
    stopFeed?.();
    stopFeed = null;
    stopClock();
    state.connected = false;
    state.status = 'Disconnected';
    draw();
  },
  onGroup: id => {
    stopClock();
    follow = state.connected;
    state.playing = follow;
    showGroup(id, true);
  },
  onFiles: async files => {
    const token = reset('Local replay');
    try {
      const loaded = await loadFiles(files);
      if (generation !== token) return;
      if (!loaded.length) throw new Error('Select the run folder or its spectator event files.');
      receive(loaded);
    } catch (error) { if (generation === token) fail(error); }
  },
  onPause: () => { stopClock(); draw(); },
  onPlay: play,
  onSeek: week => {
    stopClock();
    state.index = Math.min(state.maxIndex, Math.max(0, Math.round(week)));
    draw();
  },
});

createSimulationControls({onManifest: url => connect(url)});
connect('./demo/spectator-manifest.json', true);
