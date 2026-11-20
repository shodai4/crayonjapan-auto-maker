/**
 * CrayonJapan: Drive -> GitHub Actions relay.
 *
 * This file is the Apps Script half of the auto-editing pipeline. Paste it into
 * the same Apps Script project as the message-board relay (it only adds
 * functions; watchMessageBoard is untouched).
 *
 * Flow
 *   1. watchRawVideo() runs from a 1-minute time-driven trigger.
 *   2. It lists 01_RAW_VIDEO and looks for two kinds of work:
 *        - a subfolder renamed to GO_...  (one artwork, several raw clips)
 *        - a loose .mp4 / .mov / .avi file
 *      Folders without the GO_ prefix are still being uploaded and ignored.
 *   3. It skips items that were already dispatched, then POSTs a
 *      repository_dispatch to shodai4/crayonjapan-auto-maker with the item
 *      id/name/kind and a fresh Drive access token from ScriptApp.
 *   4. GitHub Actions renders the Short, writes the package into
 *      02_READY_TO_POST and moves the source into 03_ARCHIVE.
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
var GO_FOLDER_PATTERN = /^GO_/i;         // the "this artwork is finished" signal
var MIN_AGE_MS = 60 * 1000;              // a loose file must settle first
var FOLDER_MIN_AGE_MS = 10 * 1000;       // the GO_ rename itself is the signal
var RETRY_AFTER_MS = 6 * 60 * 60 * 1000; // an item still in RAW after 6 h may be retried
var LOG_PROPERTY = 'raw_dispatch_log';
var LOG_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * Reads 01_RAW_VIDEO and starts the render when a GO_ folder or a new video is
 * waiting. Runs every minute; safe to run by hand from the editor.
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
  var waiting = collectWaitingItems(folderId, log, now);

  if (!waiting.length) {
    return; // nothing new; stay quiet so the 1-minute trigger does not spam logs
  }

  // One dispatch is enough: the Actions run scans the folder and renders every
  // waiting item, so the rest are marked here only to keep the log accurate.
  try {
    dispatchToGitHub(waiting[0], props);
  } catch (err) {
    // Never throw from the time-driven trigger (it would mail the owner every
    // minute). Nothing is logged as dispatched, so the next sweep retries.
    console.error('[raw] dispatch failed; retrying next sweep: ' + message(err));
    return;
  }

  for (var i = 0; i < waiting.length; i++) {
    log[waiting[i].id] = now;
  }
  writeDispatchLog(props, log, now);
  console.log('[raw] dispatched ' + waiting.length + ' item(s) to GitHub Actions.');
}

/**
 * Everything in 01_RAW_VIDEO that is ready to render: GO_ folders first (an
 * artwork folder is usually several recordings at once), then loose videos.
 */
function collectWaitingItems(folderId, log, now) {
  var folder = DriveApp.getFolderById(folderId);
  var items = [];

  var subfolders = folder.getFolders();
  while (subfolders.hasNext()) {
    var sub = subfolders.next();
    if (!GO_FOLDER_PATTERN.test(sub.getName())) {
      continue; // still uploading: the folder gets the GO_ prefix when it is done
    }
    if (alreadyDispatched(log, sub.getId(), now)) {
      continue;
    }
    if (now - sub.getLastUpdated().getTime() < FOLDER_MIN_AGE_MS) {
      continue; // renamed a moment ago; give Drive a few seconds to settle
    }
    items.push({ id: sub.getId(), name: sub.getName(), kind: 'folder' });
  }

  var files = folder.getFiles();
  while (files.hasNext()) {
    var file = files.next();
    if (!VIDEO_EXTENSION.test(file.getName())) {
      continue;
    }
    if (alreadyDispatched(log, file.getId(), now)) {
      continue;
    }
    if (now - file.getLastUpdated().getTime() < MIN_AGE_MS) {
      continue; // still being uploaded
    }
    items.push({ id: file.getId(), name: file.getName(), kind: 'file' });
  }

  return items;
}

/** One-line error text for the log, without ever dumping a token. */
function message(err) {
  return err && err.message ? err.message : String(err);
}

/** True while an item is inside its idempotency window. */
function alreadyDispatched(log, id, now) {
  var dispatchedAt = log[id] || 0;
  return Boolean(dispatchedAt && now - dispatchedAt < RETRY_AFTER_MS);
}

/** POSTs repository_dispatch for one Drive folder/file and returns true on 204. */
function dispatchToGitHub(item, props) {
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
      item_id: item.id,
      item_name: item.name,
      item_type: item.kind,
      // kept for older workflow versions that only knew about single files
      file_id: item.id,
      file_name: item.name,
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
  console.log('[raw] sent ' + item.kind + ' ' + item.name + ' (' + item.id + ') to ' + repo);
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

/** Forgets the dispatched items so the next sweep retries them (handy while testing). */
function clearDispatchLog() {
  PropertiesService.getScriptProperties().deleteProperty(LOG_PROPERTY);
  console.log('[raw] dispatch log cleared; the next sweep will treat every item as new.');
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
  var folder = DriveApp.getFolderById(folderId);
  var count = 0;

  var subfolders = folder.getFolders();
  while (subfolders.hasNext()) {
    var sub = subfolders.next();
    var ready = GO_FOLDER_PATTERN.test(sub.getName());
    console.log(
      '[folder] ' + sub.getName() + ' | id=' + sub.getId() +
      ' | go=' + (ready ? 'yes' : 'no') +
      ' | age=' + Math.round((now - sub.getLastUpdated().getTime()) / 1000) + 's' +
      ' | dispatched=' + (log[sub.getId()] ? 'yes' : 'no')
    );
    if (ready) {
      count++;
    }
  }

  var files = folder.getFiles();
  while (files.hasNext()) {
    var file = files.next();
    if (!VIDEO_EXTENSION.test(file.getName())) {
      continue;
    }
    count++;
    console.log(
      '[file] ' + file.getName() + ' | id=' + file.getId() +
      ' | age=' + Math.round((now - file.getLastUpdated().getTime()) / 1000) + 's' +
      ' | dispatched=' + (log[file.getId()] ? 'yes' : 'no')
    );
  }
  console.log('[raw] ' + count + ' renderable item(s) in 01_RAW_VIDEO.');
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
