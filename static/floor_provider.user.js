// LDIS Floor Sync — pulls who is on the floor, from FCLM, in your browser.
//
// The console's server has no Midway session and must never hold one, so
// every call below runs as you, from your machine, exactly as if you had
// opened the report yourself. What reaches the server is the finished
// snapshot: who was booked to which process in the last window, and
// where they were standing.
//
// Why Function Rollup rather than Employee Time Details: Time Details
// answers for one person per request, so a 30-person team is 30 requests
// and a site is hundreds. Function Rollup answers for a whole process at
// once, so the entire floor is one request per process — a dozen, for a
// site — and it already carries the name and the manager. Time Details
// stays the right tool for one person, and the console links to it.
//
// Station data is not in FCLM. It comes from the same places the
// network's "AAs Stations for Sites" script (@aakalish, @nirichm) reads
// them: Staffing Command Center, RoboScout and Inbound Flow. That part
// is best effort — if those calls are refused, presence and process
// still work and the console says stations are unavailable.
//
// ==UserScript==
// @name         LDIS Floor Sync
// @namespace    ldis
// @version      1.1.0-__LDIS_BUILD__
// @description  Sweeps FCLM Function Rollup for who is on the floor and posts it to the L&D Operations Console.
// @match        __LDIS_MATCH__
// @grant        GM_xmlhttpRequest
// @connect      fclm-portal.amazon.com
// @connect      adapt-iad.amazon.com
// @connect      roboscout.amazon.com
// @connect      staffingcommandcenter-na.aka.amazon.com
// @connect      staffingcommandcenter-eu.aka.amazon.com
// @connect      inbound-flow-svc-iad-prod.amazon.com
// @updateURL    __LDIS_UPDATE__
// @downloadURL  __LDIS_UPDATE__
// ==/UserScript==

(function () {
    'use strict';

    var CONFIG_URL = '__LDIS_CONFIG__';
    var POST_URL = '__LDIS_POST__';
    var READER_VERSION = '1.1.0-__LDIS_BUILD__';
    var state = { running: false, lastRun: 0 };

    function log() {
        var args = ['[LDIS Floor]'].concat(Array.prototype.slice.call(arguments));
        console.log.apply(console, args);
    }

    function announce(detail) {
        document.dispatchEvent(new CustomEvent('ldis-floor-status', { detail: detail }));
    }

    /* ---------------------------------------------------------------
       FCLM: one Function Rollup per process, over an intraday window.
       --------------------------------------------------------------- */

    function pad(n) { return String(n).padStart(2, '0'); }

    function fclmDate(d) {
        return d.getFullYear() + '/' + pad(d.getMonth() + 1) + '/' + pad(d.getDate());
    }

    function rollupUrl(warehouseId, processId, from, to) {
        return 'https://fclm-portal.amazon.com/reports/functionRollup'
            + '?reportFormat=HTML'
            + '&warehouseId=' + encodeURIComponent(warehouseId)
            + '&processId=' + encodeURIComponent(processId)
            + '&maxIntradayDays=1'
            + '&spanType=Intraday'
            + '&startDateIntraday=' + encodeURIComponent(fclmDate(from))
            + '&startHourIntraday=' + from.getHours()
            + '&startMinuteIntraday=' + from.getMinutes()
            + '&endDateIntraday=' + encodeURIComponent(fclmDate(to))
            + '&endHourIntraday=' + to.getHours()
            + '&endMinuteIntraday=' + to.getMinutes();
    }

    function fetchText(url, method, body, headers) {
        return new Promise(function (resolve, reject) {
            GM_xmlhttpRequest({
                method: method || 'GET',
                url: url,
                data: body,
                headers: headers,
                timeout: 60000,
                onload: function (r) { resolve(r.responseText); },
                onerror: function (e) { reject(e); },
                ontimeout: function () { reject(new Error('timeout: ' + url)); }
            });
        });
    }

    /* The report's header is two rows: the first names the columns, and
       any column it spans is named by the second. Walking both and
       expanding the spans gives a label -> index map that survives FCLM
       adding a column, which reading fixed positions would not. */
    function headerIndex(table) {
        var rows = table.querySelectorAll('thead tr');
        if (!rows.length) return null;
        var top = rows[0].children;
        /* Only the cells that actually carry a label: a report that
           spans its first columns down both rows and one that repeats
           them as empty cells then read the same. */
        var sub = rows.length > 1
            ? Array.prototype.slice.call(rows[1].children)
                .filter(function (c) { return (c.textContent || '').trim(); })
            : [];
        var map = {};
        var flat = 0;
        var subAt = 0;
        for (var i = 0; i < top.length; i++) {
            var cell = top[i];
            var span = parseInt(cell.getAttribute('colspan') || '1', 10) || 1;
            var label = (cell.textContent || '').trim();
            if (span === 1) {
                if (label) map[label.toLowerCase()] = flat;
                flat += 1;
            } else {
                var group = label.toLowerCase();
                for (var s = 0; s < span; s++) {
                    var subLabel = subAt < sub.length ? (sub[subAt].textContent || '').trim() : '';
                    subAt += 1;
                    if (subLabel) map[(group + ' ' + subLabel).toLowerCase()] = flat;
                    flat += 1;
                }
            }
        }
        return map;
    }

    /* The function a table is reporting on is in the heading above it,
       written "RF Pick [4300000184]" — the id is FCLM's, not something
       the console has any use for. */
    function functionNameFor(table) {
        var node = table.previousElementSibling;
        var hops = 0;
        while (node && hops < 4) {
            var text = (node.textContent || '').trim();
            if (text && text.length < 120) {
                return text.replace(/\[[^\]]*\]/g, '').replace(/\s*(Small|Medium|Large|HeavyBulky|Total)\s*$/g, '').trim();
            }
            node = node.previousElementSibling;
            hops += 1;
        }
        return null;
    }

    function parseRollup(html, process) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var tables = doc.querySelectorAll('table');
        var out = [];
        for (var t = 0; t < tables.length; t++) {
            var table = tables[t];
            var map = headerIndex(table);
            if (!map || !('id' in map) || !('name' in map)) continue;
            var fn = functionNameFor(table);
            var rows = table.querySelectorAll('tbody tr');
            for (var r = 0; r < rows.length; r++) {
                var cells = rows[r].children;
                if (!cells || cells.length <= map.name) continue;
                /* The table's own total row is marked by a spanning
                   first cell, which also shifts every index after it.
                   It is a sum, not a person. */
                if (parseInt(cells[0].getAttribute('colspan') || '1', 10) > 1) continue;
                var link = rows[r].querySelector('a[href*="employeeId="]');
                var employeeId = link
                    ? (link.getAttribute('href').split('employeeId=')[1] || '').split('&')[0]
                    : (cells[map.id].textContent || '').trim();
                var name = (cells[map.name].textContent || '').trim();
                /* The last row of each table is the table's own total,
                   which has no employee and must not become a person. */
                if (!employeeId || !name || /^total$/i.test(name)) continue;
                out.push({
                    employee_id: employeeId,
                    full_name: name,
                    manager_name: 'manager' in map && cells[map.manager]
                        ? (cells[map.manager].textContent || '').trim() : null,
                    station: 'station' in map && cells[map.station]
                        ? cleanStation((cells[map.station].textContent || '').trim()) : null,
                    process_key: process.key,
                    process_name: process.label,
                    function_name: fn,
                    paid_hours: numberAt(cells, map['paid hours total'])
                });
            }
        }
        return out;
    }

    function cleanStation(value) {
        if (!value || value === '-' || value === '–') return null;
        return value;
    }

    function numberAt(cells, index) {
        if (index === undefined || !cells[index]) return null;
        var raw = (cells[index].textContent || '').trim().replace(/,/g, '');
        var n = parseFloat(raw);
        return isNaN(n) ? null : n;
    }

    /* ---------------------------------------------------------------
       Employee Time Details: one person per request, which is why it is
       not how the floor is swept. It is still the only place that says
       whether somebody is actually on the clock rather than merely
       booked to a process, so it runs as a second, bounded pass over
       the people a manager asked about — and it hands back the badge
       photo and the login in the same response, which the rollup cannot.
       --------------------------------------------------------------- */

    function timeDetailsUrl(warehouseId, employeeId, when) {
        return 'https://fclm-portal.amazon.com/employee/timeDetails'
            + '?employeeId=' + encodeURIComponent(employeeId)
            + '&warehouseId=' + encodeURIComponent(warehouseId)
            + '&spanType=Day'
            + '&startDateDay=' + encodeURIComponent(fclmDate(when));
    }

    /* FCLM writes session times as MM/DD-HH:MM:SS with no year. The year
       is the report's, and a session that runs past midnight is written
       against the next day, so the date carries. */
    function parseSessionTime(text, reference) {
        var m = /(\d{2})\/(\d{2})-(\d{2}):(\d{2}):(\d{2})/.exec(String(text || ''));
        if (!m) return null;
        var year = reference.getFullYear();
        var when = new Date(year, Number(m[1]) - 1, Number(m[2]),
                            Number(m[3]), Number(m[4]), Number(m[5]));
        /* A report run on 1 January showing 12/31 is last year's. */
        if (when - reference > 180 * 86400000) when.setFullYear(year - 1);
        if (reference - when > 180 * 86400000) when.setFullYear(year + 1);
        return when;
    }

    function infoField(doc, label) {
        var lines = doc.querySelectorAll('.employeeInfo .line, .employeeInfo span');
        for (var i = 0; i < lines.length; i++) {
            var tag = lines[i].querySelector ? lines[i].querySelector('.row-label') : null;
            if (!tag) continue;
            if ((tag.textContent || '').trim().toLowerCase() !== label) continue;
            var text = (lines[i].textContent || '').trim();
            return text.slice((tag.textContent || '').trim().length).trim();
        }
        return null;
    }

    /* FCLM writes a session as "Process<separator>Function", with its own
       glyph in the middle. Which glyph is not worth depending on, so
       anything that is not ordinary text is treated as the separator. */
    var TITLE_SEPARATOR = /[^\x20-\x7E]+/;

    function splitTaskTitle(title) {
        var parts = String(title || '').split(TITLE_SEPARATOR)
            .map(function (s) { return s.trim(); })
            .filter(Boolean);
        if (parts.length >= 2) return { process: parts[0], fn: parts.slice(1).join(' ') };
        return { process: parts[0] || null, fn: null };
    }

    /* Clock rows are the shift; breaks and time off task are states, not
       work. Telling them apart is the difference between "on Pick" and
       "punched in, off task", which is the whole question a manager has
       about somebody who is here but not on a process. */
    function taskKind(title) {
        if (/^onclock|^offclock/i.test(title)) return 'clock';
        if (/^break/i.test(title)) return 'break';
        if (/time off task|^tot\b/i.test(title)) return 'off-task';
        return 'work';
    }

    function parseTimeDetails(html, now) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var reference = now || new Date();
        var photo = doc.querySelector('.employeeInfo img');
        var out = {
            employee_login: infoField(doc, 'login'),
            employee_id: infoField(doc, 'empl id'),
            shift_code: infoField(doc, 'shift'),
            home_location: infoField(doc, 'location'),
            photo_url: photo ? photo.getAttribute('src') : null,
            clock_state: 'unknown',
            clocked_in_at: null,
            clocked_out_at: null,
            current_title: null,
            current_process: null,
            current_function: null,
            current_kind: null,
            task_started_at: null
        };

        var rows = doc.querySelectorAll('table tbody tr');
        var onClock = [];
        var offClock = [];
        var activity = [];
        for (var r = 0; r < rows.length; r++) {
            var cells = rows[r].children;
            if (!cells || cells.length < 3) continue;
            var title = (cells[0].textContent || '').trim();
            var start = parseSessionTime(cells[1].textContent, reference);
            var end = parseSessionTime(cells[2].textContent, reference);
            if (!start) continue;
            var kind = taskKind(title);
            if (kind === 'clock') {
                if (!end) continue;
                if (/^onclock/i.test(title)) onClock.push({ start: start, end: end });
                else offClock.push({ start: start, end: end });
            } else {
                activity.push({ title: title, start: start, end: end, kind: kind });
            }
        }

        /* The row with no end is the one happening now. Failing that,
           the latest one that has started — a report refreshed a moment
           after a session closed has no open row at all. */
        var current = null;
        activity.forEach(function (row) {
            if (row.end) return;
            if (!current || row.start > current.start) current = row;
        });
        if (!current) {
            activity.forEach(function (row) {
                if (row.start > reference) return;
                if (!current || row.start > current.start) current = row;
            });
        }
        if (current) {
            var split = splitTaskTitle(current.title);
            out.current_title = current.title;
            out.current_process = split.process;
            out.current_function = split.fn;
            out.current_kind = current.kind;
            out.task_started_at = current.start.toISOString();
        }

        if (!onClock.length && !offClock.length) return out;

        /* A session that has not ended yet is written with an end at the
           next midnight, so "covers now" is what distinguishes somebody
           still on the clock from somebody whose shift is recorded and
           finished. */
        var covering = null;
        onClock.concat(offClock).forEach(function (session) {
            if (session.start <= reference && reference < session.end) {
                if (!covering || session.start > covering.start) covering = session;
            }
        });
        var latestOn = onClock.reduce(function (a, b) {
            return !a || b.start > a.start ? b : a;
        }, null);

        if (covering && onClock.indexOf(covering) > -1) {
            out.clock_state = 'in';
            out.clocked_in_at = covering.start.toISOString();
        } else if (covering) {
            out.clock_state = 'out';
            if (latestOn) out.clocked_out_at = latestOn.end.toISOString();
        } else if (latestOn && latestOn.end > reference) {
            out.clock_state = 'in';
            out.clocked_in_at = latestOn.start.toISOString();
        } else if (latestOn) {
            out.clock_state = 'out';
            out.clocked_out_at = latestOn.end.toISOString();
        }
        return out;
    }

    /* ---------------------------------------------------------------
       Stations. Not an FCLM report — the same three sources the
       network's stations script reads. Every one of them is optional.
       --------------------------------------------------------------- */

    function stationsFromSCC(warehouseId, region) {
        var base = 'https://staffingcommandcenter-' + region + '.aka.amazon.com/';
        return fetchText(base + 'getLatestGeneratedPlanRecord/' + warehouseId)
            .then(function (text) {
                var plan = JSON.parse(text);
                return fetchText(base + 'getPlanIntervals/' + plan.planId + '/' + warehouseId + '/Singles')
                    .then(function (intervalText) {
                        var intervals = JSON.parse(intervalText);
                        var now = new Date();
                        var chosen = null;
                        for (var i = 0; i < intervals.length; i++) {
                            if (intervals[i].startTime * 1000 <= now
                                && (!chosen || intervals[i].startTime > chosen.startTime)) {
                                chosen = intervals[i];
                            }
                        }
                        if (!chosen) return {};
                        return fetchText(
                            base + 'getStaffingPlansForWorkInterval/' + plan.planId + '/Singles',
                            'POST', JSON.stringify(chosen),
                            { 'Accept': '*/*', 'content-type': 'application/json' }
                        ).then(function (planText) {
                            var data = JSON.parse(planText);
                            var byId = {};
                            var map = data.employeeIdToStationsMap || {};
                            Object.keys(map).forEach(function (id) {
                                if (map[id] && map[id][0]) byId[id] = map[id][0].scannableId;
                            });
                            return byId;
                        });
                    });
            });
    }

    function stationsFromRoboScout(warehouseId) {
        var urls = [2078, 2080, 2081, 2079].map(function (instance) {
            return 'https://roboscout.amazon.com/view_plot_data/?sites=(' + warehouseId
                + ')&instance_id=' + instance
                + '&object_id=20672&BrowserTZ=America%2FNew_York&app_name=RoboScout';
        });
        return Promise.all(urls.map(function (url) {
            return fetchText(url).then(function (t) { return JSON.parse(t); })
                .catch(function () { return null; });
        })).then(function (results) {
            var byLogin = {};
            results.forEach(function (data) {
                if (!data || !data.data) return;
                var rows = {};
                data.data.forEach(function (point) {
                    if (['Station_Id', 'Associate Login', 'StationType', 'Floor'].indexOf(point.key) === -1) return;
                    var value = String(point.yValue);
                    if (value.indexOf('>') > -1) value = value.split('>')[1].split('<')[0];
                    rows[point.xValue] = rows[point.xValue] || {};
                    rows[point.xValue][point.key] = value;
                });
                Object.keys(rows).forEach(function (k) {
                    var row = rows[k];
                    if (!row['Associate Login']) return;
                    byLogin[String(row['Associate Login']).toLowerCase()] =
                        String(row.Floor || '').replace('paKiva', '')
                        + ' - ' + String(row.StationType || '').charAt(0) + (row.Station_Id || '');
                });
            });
            return byLogin;
        });
    }

    function attachStations(rows, warehouseId) {
        var byId = {};
        var byLogin = {};
        var attempts = [
            stationsFromSCC(warehouseId, 'eu').then(function (m) { Object.assign(byId, m); })
                .catch(function () { return stationsFromSCC(warehouseId, 'na')
                    .then(function (m) { Object.assign(byId, m); }).catch(function () {}); }),
            stationsFromRoboScout(warehouseId).then(function (m) { Object.assign(byLogin, m); })
                .catch(function () {})
        ];
        return Promise.all(attempts).then(function () {
            var attached = 0;
            rows.forEach(function (row) {
                var station = byId[row.employee_id]
                    || (row.employee_login ? byLogin[row.employee_login.toLowerCase()] : null);
                if (station && !row.station) { row.station = station; attached += 1; }
            });
            return attached;
        });
    }

    /* ---------------------------------------------------------------
       Employee ids to logins. The console knows people by login; FCLM
       reports them by id. Resolving here means the server never has to
       guess from a name.
       --------------------------------------------------------------- */

    var LOGIN_CACHE_KEY = 'ldisFloorLogins';

    function cachedLogins() {
        try { return JSON.parse(localStorage.getItem(LOGIN_CACHE_KEY) || '{}'); }
        catch (e) { return {}; }
    }

    function rememberLogins(map) {
        try {
            var merged = Object.assign(cachedLogins(), map);
            localStorage.setItem(LOGIN_CACHE_KEY, JSON.stringify(merged));
        } catch (e) { /* a full or blocked store is not a reason to fail the sweep */ }
    }

    function resolveLogins(rows) {
        var cache = cachedLogins();
        var unknown = [];
        rows.forEach(function (row) {
            if (cache[row.employee_id]) { row.employee_login = cache[row.employee_id]; }
            else if (unknown.indexOf(row.employee_id) === -1) { unknown.push(row.employee_id); }
        });
        if (!unknown.length) return Promise.resolve(0);

        var batches = [];
        for (var i = 0; i < unknown.length; i += 50) batches.push(unknown.slice(i, i + 50));
        var learned = {};
        return Promise.all(batches.map(function (batch) {
            var url = 'https://adapt-iad.amazon.com/api/employee-profile-svc/GetEmployeeProfiles'
                + '?employeeLogins=' + encodeURIComponent(JSON.stringify(batch));
            return fetchText(url).then(function (text) {
                var data = JSON.parse(text);
                Object.keys(data).forEach(function (key) {
                    var profile = data[key] || {};
                    if (profile.login) {
                        learned[String(profile.employeeId || key)] = profile.login;
                    }
                });
            }).catch(function () { /* no profile service, fall back to names */ });
        })).then(function () {
            rememberLogins(learned);
            rows.forEach(function (row) {
                if (!row.employee_login && learned[row.employee_id]) {
                    row.employee_login = learned[row.employee_id];
                }
            });
            return Object.keys(learned).length;
        });
    }

    /* ---------------------------------------------------------------
       The clock pass. One request per person, so it is deliberately
       limited to the team the page asked about and run a few at a time:
       the point is to answer "punched in or not" without making FCLM
       serve three hundred pages because somebody opened a tab.
       --------------------------------------------------------------- */

    var CLOCK_CONCURRENCY = 4;

    function runPooled(items, size, worker) {
        var index = 0;
        var results = [];
        function next() {
            if (index >= items.length) return Promise.resolve();
            var mine = index++;
            return Promise.resolve(worker(items[mine], mine))
                .then(function (value) { if (value) results.push(value); })
                .catch(function () { /* one person failing is not the pass failing */ })
                .then(next);
        }
        var runners = [];
        for (var i = 0; i < Math.min(size, items.length); i++) runners.push(next());
        return Promise.all(runners).then(function () { return results; });
    }

    function clockPass(config, scope) {
        var rosterUrl = config.roster_url + (scope ? '?am=' + encodeURIComponent(scope) : '');
        return fetch(rosterUrl, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var roster = data.roster || [];
                if (!roster.length) return { stored: 0, asked: 0 };
                announce({ phase: 'clock', done: 0, total: roster.length });
                var done = 0;
                var now = new Date();
                return runPooled(roster, CLOCK_CONCURRENCY, function (person) {
                    return fetchText(timeDetailsUrl(config.warehouse_id, person.employee_id, now))
                        .then(function (html) {
                            var parsed = parseTimeDetails(html, new Date());
                            parsed.employee_login = parsed.employee_login || person.employee_login;
                            parsed.employee_id = parsed.employee_id || person.employee_id;
                            parsed.full_name = person.full_name;
                            return parsed;
                        })
                        .then(function (parsed) {
                            done += 1;
                            announce({ phase: 'clock', done: done, total: roster.length });
                            return parsed;
                        });
                }).then(function (entries) {
                    if (!entries.length) return { stored: 0, asked: roster.length };
                    return fetch(config.clock_url, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ entries: entries })
                    }).then(function (r) { return r.json(); });
                });
            });
    }

    /* --------------------------------------------------------------- */

    function sweep(force) {
        if (state.running) return Promise.resolve();
        state.running = true;
        announce({ phase: 'starting' });

        return fetch(CONFIG_URL, { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (config) {
                var to = new Date();
                var from = new Date(to.getTime() - (config.window_minutes || 30) * 60000);
                var processes = config.processes || [];
                announce({ phase: 'fetching', total: processes.length });

                var rows = [];
                var done = 0;
                return runPooled(processes, 3, function (process) {
                    return fetchText(rollupUrl(config.warehouse_id, process.process_id, from, to))
                        .then(function (html) {
                            rows = rows.concat(parseRollup(html, process));
                        })
                        .catch(function (e) { log('process failed', process.key, e); })
                        .then(function () {
                            done += 1;
                            announce({ phase: 'fetching', done: done, total: processes.length });
                        });
                }).then(function () {
                    announce({ phase: 'resolving', rows: rows.length });
                    return resolveLogins(rows);
                }).then(function () {
                    if (!config.stations) return 0;
                    announce({ phase: 'stations' });
                    return attachStations(rows, config.warehouse_id).catch(function () { return 0; });
                }).then(function () {
                    announce({ phase: 'posting', rows: rows.length });
                    return fetch(POST_URL, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            window_start: from.toISOString(),
                            window_end: to.toISOString(),
                            reader_version: READER_VERSION,
                            rows: rows
                        })
                    }).then(function (r) { return r.json(); });
                }).then(function (result) {
                    log('swept', result);
                    if (!config.clock) return result;
                    var marker = document.getElementById('ldisFloorMarker');
                    var scope = marker ? marker.dataset.scope : null;
                    return clockPass(config, scope)
                        .catch(function (e) { log('clock pass failed', e); return null; })
                        .then(function (clock) {
                            result.clock = clock;
                            return result;
                        });
                }).then(function (result) {
                    state.lastRun = Date.now();
                    announce({ phase: 'done', result: result });
                    document.dispatchEvent(new CustomEvent('ldis-floor-updated', { detail: result }));
                });
            })
            .catch(function (e) {
                log('sweep failed', e);
                announce({ phase: 'failed', error: String(e && e.message || e) });
            })
            .then(function () { state.running = false; });
    }

    window.ldisFloorSweep = sweep;
    document.addEventListener('ldis-floor-sweep', function () { sweep(true); });

    /* A page that wants the floor says so by carrying the marker; the
       script does nothing on any other page in the console. */
    function autoRun() {
        if (!document.getElementById('ldisFloorMarker')) return;
        announce({ phase: 'present' });
        var stamp = Number(document.getElementById('ldisFloorMarker').dataset.ageSeconds || '99999');
        if (stamp > 240) sweep(false);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', autoRun);
    } else {
        autoRun();
    }
})();
