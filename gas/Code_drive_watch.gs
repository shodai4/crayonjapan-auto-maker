/**
 * CrayonJapan: Drive -> GitHub Actions relay.
 *
 * This file is the Apps Script half of the auto-editing pipeline. Paste it into
 * the same Apps Script project as the message-board relay (it only adds
 * functions; watchMessageBoard is untouched).
 *
 * Flow
 *   1. watchRawVideo() runs from a 1-minute time-driven trigger.
 *   2. It lists 01_RAW_VIDEO, skips files younger than a minute (the phone
 *      upload is still finishing) and files that were already dispatched.
 *   3. It POSTs a repository_dispatch to shodai4/crayonjapan-auto-maker with the
 *      file id/name and a fresh Drive access token from ScriptApp.
 *   4. GitHub Actions renders the video, writes the package into
 *      02_READY_TO_POST and moves the raw file into 03_ARCHIVE.
 *
 * Script properties this file expects (Project settings -> Script properties):
 *   RAW_FOLDER_ID   id of 01_RAW_VIDEO                      (required)
 *   GITHUB_TOKEN    fine-grained PAT for that repository     (required)
 *                   Contents: read and write (GitHub only accepts a
 *                   repository_dispatch from a token that can write code);
 *                   nothing else. Revoke it here when the relay is retired.
 *   GITHUB_REPO     "owner/repo", defaults to shodai4/crayonjapan-auto-maker
 *
 * Nothing secret is written into this file, so it can stay in the public
 * repository for review.
 */

var DISPATCH_EVENT_TYPE = 'raw_video_uploaded';
var DEFAULT_GITHUB_REPO = 'shodai4/crayonjapan-auto-maker';
var VIDEO_EXTENSION = /\.(mp4|mov|avi)$/i;
var MIN_AGE_MS = 60 * 1000;              // let the upload settle first
var RETRY_AFTER_MS = 6 * 60 * 60 * 1000; // a file still in RAW after 6 h may be retried
var MAX_DISPATCH_PER_SWEEP = 1;          // one run renders every waiting file anyway
var LOG_PROPERTY = 'raw_dispatch_log';
var LOG_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * Reads 01_RAW_VIDEO and starts the render when something new is waiting.
 * Runs every minute; safe to run by hand from the editor.
 */
function watchRawVideo() {
  var props = PropertiesService.getScriptProperties();
  var folderId = props.getProperty('RAW_FOLDER_ID');
  if (!folderId) {
    // Log instead of throwing: an exception here would mail the owner once a
    // minute from the time-driven trigger.
    console.error('[raw] RAW_FOLDER_ID is not set in the script properties.');
    return;
  }
  if (!props.getProperty('GITHUB_TOKEN')) {
    console.error('[raw] GITHUB_TOKEN is not set; the relay is paused.');
    return;
  }

  var log = readDispatchLog(props);
  var now = Date.now();
  var waiting = [];
  var files = DriveApp.getFolderById(folderId).getFiles();

  while (files.hasNext()) {
    var file = files.next();
    if (!VIDEO_EXTENSION.test(file.getName())) {
      continue;
    }
    var id = file.getId();
    var dispatchedAt = log[id] || 0;
    if (dispatchedAt && now - dispatchedAt < RETRY_AFTER_MS) {
      continue; // already handed to GitHub (or a run is still going)
    }
    if (now - file.getLastUpdated().getTime() < MIN_AGE_MS) {
      continue; // still being uploaded
    }
    waiting.push(file);
    if (waiting.length >= MAX_DISPATCH_PER_SWEEP) {
      break;
    }
  }

  if (!waiting.length) {
    return; // nothing new; stay quiet so the 1-minute trigger does not spam logs
  }

  dispatchToGitHub(waiting[0], props);

  for (var i = 0; i < waiting.length; i++) {
    log[waiting[i].getId()] = now;
  }
  writeDispatchLog(props, log, now);
  console.log('[raw] dispatched ' + waiting.length + ' file(s) to GitHub Actions.');
}

/** POSTs repository_dispatch for one Drive file and returns true on 204. */
function dispatchToGitHub(file, props) {
  var token = props.getProperty('GITHUB_TOKEN');
  if (!token) {
    console.error(
      '[raw] GITHUB_TOKEN is not set. Create a fine-grained personal access token for ' +
      props.getProperty('GITHUB_REPO') + ' with Contents: read and write, and nothing else.'
    );
    return false;
  }
  var repo = props.getProperty('GITHUB_REPO') || DEFAULT_GITHUB_REPO;
  var payload = {
    event_type: DISPATCH_EVENT_TYPE,
    client_payload: {
      file_id: file.getId(),
      file_name: file.getName(),
      dispatched_at: new Date().toISOString(),
      drive_token: ScriptApp.getOAuthToken()
    }
  };

  var response = UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/dispatches', {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  var code = response.getResponseCode();
  if (code !== 204) {
    // Never echo the token; GitHub's own message is enough to debug the scope.
    throw new Error(
      'GitHub dispatch failed (' + code + '): ' + response.getContentText().slice(0, 300)
    );
  }
  console.log('[raw] sent ' + file.getName() + ' (' + file.getId() + ') to ' + repo);
  return true;
}

/** Creates the 1-minute trigger once; safe to call repeatedly. */
function installRawVideoTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'watchRawVideo') {
      console.log('[raw] trigger already installed.');
      return 'exists';
    }
  }
  ScriptApp.newTrigger('watchRawVideo').timeBased().everyMinutes(1).create();
  console.log('[raw] installed the 1-minute watchRawVideo trigger.');
  return 'installed';
}

/** Forgets the dispatched files so the next sweep retries them (handy while testing). */
function clearDispatchLog() {
  PropertiesService.getScriptProperties().deleteProperty(LOG_PROPERTY);
  console.log('[raw] dispatch log cleared; the next sweep will treat every file as new.');
  return 'cleared';
}

/** Prints what the next sweep would see; useful for a first manual check. */
function describeRawFolder() {
  var props = PropertiesService.getScriptProperties();
  var folderId = props.getProperty('RAW_FOLDER_ID');
  if (!folderId) {
    throw new Error('RAW_FOLDER_ID is not set in the script properties.');
  }
  var log = readDispatchLog(props);
  var now = Date.now();
  var files = DriveApp.getFolderById(folderId).getFiles();
  var count = 0;
  while (files.hasNext()) {
    var file = files.next();
    if (!VIDEO_EXTENSION.test(file.getName())) {
      continue;
    }
    count++;
    console.log(
      file.getName() + ' | id=' + file.getId() +
      ' | age=' + Math.round((now - file.getLastUpdated().getTime()) / 1000) + 's' +
      ' | dispatched=' + (log[file.getId()] ? 'yes' : 'no')
    );
  }
  console.log('[raw] ' + count + ' video file(s) in 01_RAW_VIDEO.');
  return count;
}

function readDispatchLog(props) {
  var raw = props.getProperty(LOG_PROPERTY);
  if (!raw) {
    return {};
  }
  try {
    var parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    return {};
  }
}

function writeDispatchLog(props, log, now) {
  var pruned = {};
  for (var id in log) {
    if (Object.prototype.hasOwnProperty.call(log, id) && now - log[id] < LOG_RETENTION_MS) {
      pruned[id] = log[id];
    }
  }
  props.setProperty(LOG_PROPERTY, JSON.stringify(pruned));
}
