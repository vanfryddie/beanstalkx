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
// @version      1.0.0-__LDIS_BUILD__
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
                return processes.reduce(function (chain, process) {
                    return chain.then(function () {
                        return fetchText(rollupUrl(config.warehouse_id, process.process_id, from, to))
                            .then(function (html) {
                                rows = rows.concat(parseRollup(html, process));
                            })
                            .catch(function (e) { log('process failed', process.key, e); })
                            .then(function () {
                                done += 1;
                                announce({ phase: 'fetching', done: done, total: processes.length });
                            });
                    });
                }, Promise.resolve()).then(function () {
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
                            rows: rows
                        })
                    }).then(function (r) { return r.json(); });
                }).then(function (result) {
                    state.lastRun = Date.now();
                    log('swept', result);
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
