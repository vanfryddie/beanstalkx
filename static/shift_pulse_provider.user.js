// Shift Pulse - Data Provider, as shipped with the EU PE NET standard
// (B2B Action #3.5). Only the metadata block differs, and only in the
// three placeholders the console fills in when it serves this file:
// @match points at this L&D Operations Console instead of a local
// Shift-Pulse.html, @updateURL/@downloadURL point back here, and the
// version carries a digest of the build. The FCLM calls below are
// untouched and still run as you, in your browser, under your own
// Midway session - the console's server never sees FCLM.

// ==UserScript==
// @name         Shift Pulse - Data Provider
// @namespace    http://tampermonkey.net/
// @version      1.1.0-ldis.__LDIS_BUILD__
// @author       Kiro
// @description  [LDIS-hosted build] Manager-level overview without associate details or idle time. Based on Master Eagle Eye v6.5.0. Locale-aware number parsing.
// @match        __LDIS_MATCH__
// @grant        GM_xmlhttpRequest
// @connect      fclm-portal.amazon.com
// @updateURL    __LDIS_UPDATE__
// @downloadURL  __LDIS_UPDATE__
// ==/UserScript==

(function() {
    'use strict';
    
    console.log('✓ Shift Pulse Data Provider v1.1.0 loaded');

    // ── Locale Detection & Number Normalization ──
    const detectedLocale = navigator.language || navigator.userLanguage || 'en-US';
    const isEuropeanLocale = (() => {
        const testNum = (1.1).toLocaleString(detectedLocale);
        return testNum.includes(',');
    })();
    console.log(`✓ Detected locale: ${detectedLocale}, European number format: ${isEuropeanLocale}`);

    function normalizeNumber(str) {
        if (typeof str === 'number') return str;
        if (!str || typeof str !== 'string') return 0;
        str = str.trim();
        if (!str) return 0;

        let sign = '';
        if (str.startsWith('-')) {
            sign = '-';
            str = str.substring(1);
        } else if (str.startsWith('+')) {
            str = str.substring(1);
        }

        const dots = (str.match(/\./g) || []).length;
        const commas = (str.match(/,/g) || []).length;

        let cleaned;
        if (dots > 0 && commas > 0) {
            const lastDot = str.lastIndexOf('.');
            const lastComma = str.lastIndexOf(',');
            if (lastComma > lastDot) {
                cleaned = str.replace(/\./g, '').replace(',', '.');
            } else {
                cleaned = str.replace(/,/g, '');
            }
        } else if (commas > 0 && dots === 0) {
            if (commas === 1) {
                const afterComma = str.split(',')[1];
                if (afterComma && afterComma.length === 3 && /^\d{3}$/.test(afterComma)) {
                    cleaned = str.replace(',', '');
                } else {
                    cleaned = str.replace(',', '.');
                }
            } else {
                cleaned = str.replace(/,/g, '');
            }
        } else if (dots > 0 && commas === 0) {
            if (dots === 1) {
                const afterDot = str.split('.')[1];
                if (afterDot && afterDot.length === 3 && /^\d{3}$/.test(afterDot) && str.split('.')[0].length > 0) {
                    if (isEuropeanLocale) {
                        cleaned = str.replace('.', '');
                    } else {
                        cleaned = str;
                    }
                } else {
                    cleaned = str;
                }
            } else {
                cleaned = str.replace(/\./g, '');
            }
        } else {
            cleaned = str;
        }

        const result = parseFloat(sign + cleaned);
        return isNaN(result) ? 0 : result;
    }

    // Time Off Task data storage
    let timeOffTaskData = {
        employees: [],
        managerSummary: []
    };

    function getWarehouseId() {
        if (typeof window.getWarehouseId === 'function') {
            try {
                const fc = window.getWarehouseId();
                if (fc) return fc;
            } catch (e) {
                console.warn('Error calling window.getWarehouseId:', e);
            }
        }

        const fcInput = document.getElementById('fcInput');
        if (fcInput && fcInput.value) {
            const fc = fcInput.value.trim().toUpperCase();
            if (fc) {
                return fc;
            }
        }

        const savedFC = localStorage.getItem('warehouseId');
        if (savedFC) {
            return savedFC;
        }

        console.error('❌ No FC code found!');
        return null;
    }

    let employeeData = {
        opsEngagement: [],
        hrRelated: [],
        nonInventory: [],
        facJanitorialProjects5S: [],
        jambuster: [],
        totePrep: [],
        overstaffing: [],
        podBanding: [],
        sevEvents: []
    };

    let hourlyData = [];
    let processPathData = [];
    let summaryData = null;

    let summaryTargets = {
        opsEngagement: 0,
        hrRelated: 0,
        nonInventory: 0,
        facJanitorialProjects5S: 0,
        jambuster: 0,
        totePrep: 0,
        overstaffing: 0,
        podBanding: 0,
        sevEvents: 0
    };

    let hourlyIndirectTargets = {};
    let hourlyProcessPathTargets = {};

    let totalFetchSteps = 0;
    let currentFetchStep = 0;
    let currentHour = new Date().getHours();

    let lastUpdateTime = null;
    let countdownInterval = null;
    let autoRefreshTimer = null;

    let failedSlots = [];
    const MAX_RETRIES = 3;

    // Helper function to force browser reflow after table updates
    // This prevents rendering issues where content disappears during scroll
    function forceReflow(element) {
        if (element) {
            // Force a reflow by reading a layout property
            void element.offsetHeight;
        }
    }

    // Process ID mapping (matches HTML)
    const PROCESS_MAP = {
        admin: { id: '01002960', name: 'Admin/HR/IT' },
        facilities: { id: '01003028', name: 'Facilities' },
        nonfc: { id: '01003047', name: 'Non FC Controllable' }
    };

    // Get bucket configuration from HTML page or use defaults
    function getBucketConfig() {
        // Try unsafeWindow first (Tampermonkey's way to access page's window)
        if (typeof unsafeWindow !== 'undefined' && typeof unsafeWindow.getBucketConfig === 'function') {
            return unsafeWindow.getBucketConfig();
        }
        // Fallback to regular window (in case script runs in page context)
        if (typeof window.getBucketConfig === 'function') {
            return window.getBucketConfig();
        }
        // Default configuration if HTML function not available
        return {
            buckets: [
                { id: 'opsEngagement', name: 'Ops Engage', functions: [{ name: 'OPS_EMP_ENGAGEMENT', process: 'admin' }] },
                { id: 'hrRelated', name: 'HR-related', functions: [
                    { name: 'Admin Meetings', process: 'admin' },
                    { name: 'HR_GROUP_EVENTS', process: 'admin' },
                    { name: 'HR_OTHER/MISC', process: 'admin' },
                    { name: 'HR_SITE_ACCOMM', process: 'admin' }
                ]},
                { id: 'nonInventory', name: 'Non Inv', functions: [{ name: 'NonInventory', process: 'admin' }] },
                { id: 'facJanitorialProjects5S', name: 'Fac Jan/Proj/5S', functions: [
                    { name: 'Facilities 5S', process: 'facilities' },
                    { name: 'Facilities Projects', process: 'facilities' },
                    { name: 'Facility Janitorial', process: 'facilities' }
                ]},
                { id: 'jambuster', name: 'Jambuster', functions: [{ name: 'Jambusters', process: 'facilities' }] },
                { id: 'totePrep', name: 'Tote Prep', functions: [{ name: 'Tote Prep', process: 'facilities' }] },
                { id: 'overstaffing', name: 'Overstaffing', functions: [
                    { name: 'CAPEX Projects 7', process: 'nonfc' },
                    { name: 'CAPEX Projects 8', process: 'nonfc' },
                    { name: 'NON_FC//Overstaffing', process: 'nonfc' }
                ]},
                { id: 'podBanding', name: 'Pod Band', functions: [{ name: 'Pod Banding', process: 'admin' }] },
                { id: 'sevEvents', name: 'SEV events', functions: [
                    { name: 'Network SEV1/2 Event', process: 'nonfc' },
                    { name: 'RME SEV1/2 Event', process: 'nonfc' },
                    { name: 'Weather SEV1/2 Event', process: 'nonfc' }
                ]}
            ]
        };
    }

    // Calculate bucket hours from HTML responses using bucket config
    function calculateBucketHours(htmlByProcess, bucketConfig) {
        const result = {};
        
        bucketConfig.buckets.forEach(bucket => {
            let totalHours = 0;
            bucket.functions.forEach(fn => {
                const processKey = fn.process;
                const html = htmlByProcess[processKey];
                if (html) {
                    const hours = normalizeNumber(parseHoursFromHTML(html, fn.name));
                    totalHours += hours;
                }
            });
            result[bucket.id] = totalHours.toFixed(2);
        });
        
        return result;
    }

    // Calculate bucket employee data from HTML responses using bucket config
    function calculateBucketEmployees(htmlByProcess, bucketConfig) {
        const result = {};
        
        bucketConfig.buckets.forEach(bucket => {
            const employees = [];
            bucket.functions.forEach(fn => {
                const processKey = fn.process;
                const html = htmlByProcess[processKey];
                if (html) {
                    const empData = parseEmployeeData(html, fn.name);
                    employees.push(...empData);
                }
            });
            result[bucket.id] = employees;
        });
        
        return result;
    }

    // Parse functions and hours from FRU HTML for discovery
    function parseFunctionsFromFRUForDiscovery(html, processKey) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const functions = [];
        
        // List of known non-function table names to skip
        const skipNames = [
            'Function Totals',
            'Summary Small Medium Large HeavyBulky Total',
            'Summary Small Medium Large HeavyBulky Tota',
            'Summary',
            'Total',
            'Totals'
        ];
        
        // Find all function tables (they have captions with function names)
        const tables = doc.querySelectorAll('table');
        
        tables.forEach(table => {
            const caption = table.querySelector('caption');
            if (!caption) return;
            
            const captionText = caption.textContent.trim();
            // Extract function name (before the [id])
            const functionName = captionText.split('[')[0].trim();
            if (!functionName) return;
            
            // Skip summary/header tables
            if (skipNames.some(skip => functionName.toLowerCase().includes(skip.toLowerCase()))) return;
            if (functionName.toLowerCase().startsWith('summary')) return;
            if (functionName.toLowerCase() === 'total' || functionName.toLowerCase() === 'totals') return;
            
            // Skip if the caption doesn't have a numeric ID in brackets (real functions have IDs like [1234567890])
            if (!captionText.includes('[') || !captionText.match(/\[\d+\]/)) return;
            
            // Find the total row to get hours
            const totalRow = table.querySelector('tr.total, tr.empl-all.total');
            let hours = 0;
            
            if (totalRow) {
                // Look for the Paid Hours Total cell
                // It's typically a td.size-total that's NOT .numeric (numeric ones are volume)
                const cells = totalRow.querySelectorAll('td');
                cells.forEach(cell => {
                    // The hours cell usually has class "size-total" but not "numeric"
                    if (cell.classList.contains('size-total') && !cell.classList.contains('numeric')) {
                        const parsed = normalizeNumber(cell.textContent.trim());
                        if (parsed > 0) {
                            hours = parsed;
                        }
                    }
                });
                
                // Fallback: try to find by position (usually 5th or 6th cell after name/manager)
                if (hours === 0 && cells.length > 5) {
                    for (let i = 4; i < Math.min(cells.length, 8); i++) {
                        const parsed = normalizeNumber(cells[i].textContent.trim());
                        // Hours are typically < 10000 for a month, volumes are larger
                        if (parsed > 0 && parsed < 50000) {
                            hours = parsed;
                            break;
                        }
                    }
                }
            }
            
            // Only include functions with actual hours
            if (hours > 0) {
                functions.push({
                    name: functionName,
                    process: processKey,
                    hours: Math.round(hours * 100) / 100
                });
            }
        });
        
        // Sort by hours descending
        functions.sort((a, b) => b.hours - a.hours);
        
        return functions;
    }

    // OB Direct Process Configuration for Function Rollup Modal
    const OB_DIRECT_PROCESSES = {
        pick: {
            processId: '1003001',
            name: 'Pick',
            functions: ['Pick To Rebin', 'RF Pick']
        },
        packSingle: {
            processId: '1002993',
            name: 'Pack Single',
            functions: ['Scan Verify Large', 'Scan Verify Medium', 'Scan Verify Small', 'Slam At Pack']
        },
        packMultis: {
            processId: '1002991',
            name: 'Pack Multis',
            functions: ['Scan Verify Large', 'Scan Verify Medium']
        },
        spp: {
            processId: '1002989',
            name: 'SPP',
            functions: ['SmartPaper']
        },
        sort: {
            processId: '1003051',
            name: 'Sort',
            functions: ['AFE 1 Rebin', 'AFE1 Induct']
        },
        chuting: {
            processId: '1002969',
            name: 'Chuting',
            functions: ['Scan Verify AFE']
        }
    };

    // Inbound Process Configuration for Function Rollup Modal
    const IB_PROCESSES = {
        eachTsi: {
            processId: '1002976',
            name: 'Each TSI',
            functions: ['Stow Each Nike', 'Stow Each Nike Light']
        },
        tsiDecant: {
            processId: '1003019',
            name: 'TSI Decant',
            functions: ['Decant', 'Pallet_decant_split']
        },
        eachStp: {
            processId: '1003016',
            name: 'Each StP',
            functions: ['Stow Each Nike', 'Stow Each Nike Light']
        },
        prepReceiver: {
            processId: '1003002',
            name: 'Prep Receiver',
            functions: ['Prep Receive']
        },
        cubiscan: {
            processId: '1002971',
            name: 'Cubiscan',
            functions: ['Cubiscan', 'ItemDataScan', 'WeightMeasurement']
        }
    };

    // OB Indirect Process Configuration for Function Breakdown Modal
    // These use the parent process IDs and show all functions within that process
    const OB_INDIRECT_PROCESSES = {
        packSupport: {
            processId: '1002994',  // Pack Support process
            name: 'Pack Support',
            pprLineItemId: 'ppr.detail.outbound.pack.packSupport'
        },
        obLeadPA: {
            processId: '1002987',  // OB Lead/PA process
            name: 'OB Lead/PA',
            pprLineItemId: 'ppr.detail.outbound.obLeadPA.obLeadPA'
        },
        obProblemSolve: {
            processId: '1002985',  // OB Problem Solve process
            name: 'OB Problem Solve',
            pprLineItemId: 'ppr.detail.outbound.obDefect.obProblemSolve'
        },
        pickSupport: {
            processId: '1003049',  // Pick Support process
            name: 'Pick Support',
            pprLineItemId: 'ppr.detail.outbound.pick.pickSupport'
        }
    };

    // IB Indirect Process Configuration for Function Breakdown Modal
    const IB_INDIRECT_PROCESSES = {
        receiveDock: {
            processId: '1003010',  // Receive Dock process
            name: 'Receive Dock',
            pprLineItemId: 'ppr.detail.inbound.receive.receiveDock'
        },
        receiveSupport: {
            processId: '1003033',  // Receive Support process
            name: 'Receive Support',
            pprLineItemId: 'ppr.detail.inbound.receive.receiveSupport'
        },
        tsiSupport: {
            processId: '1003020',  // TSI Support process
            name: 'TSI Support',
            pprLineItemId: 'ppr.detail.inbound.stow.tsiSupport'
        },
        stpSupport: {
            processId: '1003017',  // StP Support process
            name: 'StP Support',
            pprLineItemId: 'ppr.detail.inbound.stow.stpSupport'
        },
        rsrSupport: {
            processId: '1003012',  // RSR Support process
            name: 'RSR Support',
            pprLineItemId: 'ppr.detail.inbound.rsr.rsrSupport'
        },
        ibLead: {
            processId: '1002982',  // IB Lead process
            name: 'IB Lead',
            pprLineItemId: 'ppr.detail.inbound.ibLead.ibLead'
        }
    };

    // TSO Indirect Process Configuration for Function Breakdown Modal
    const TSO_INDIRECT_PROCESSES = {
        transferOut: {
            processId: '1003021',  // Transfer Out process
            name: 'Transfer Out',
            pprLineItemId: 'ppr.detail.da.transferOut.transferOut'
        },
        transferOutDock: {
            processId: '1003022',  // TSO Dock process
            name: 'TSO Dock',
            pprLineItemId: 'ppr.detail.da.transferOutDock.transferOutDock'
        },
        toLeadPA: {
            processId: '1003023',  // TO Lead process
            name: 'TO Lead/PA',
            pprLineItemId: 'ppr.detail.da.toLeadPa.toLeadPa'
        }
    };

    // TSO Direct Process Configuration for Function Rollup Modal
    const TSO_DIRECT_PROCESSES = {
        transferOutPick: {
            processId: '1003065',
            name: 'Transfer Out Pick',
            functions: []
        }
    };

    // VRET Indirect Process Configuration for Function Breakdown Modal
    const VRET_INDIRECT_PROCESSES = {
        vretSupport: {
            processId: '1003059',  // VRET Support process
            name: 'VRET Support',
            pprLineItemId: 'ppr.detail.reverseLogistics.vReturn.vReturnSupport'
        }
    };

    // VRET Direct Process Configuration for Function Rollup Modal
    const VRET_DIRECT_PROCESSES = {
        vretPick: {
            processId: '1003034',
            name: 'VRET Pick',
            functions: []
        },
        vretPack: {
            processId: '1003056',
            name: 'VRET Pack',
            functions: []
        }
    };

    // Function Rollup Modal Functions
    function buildFunctionRollupURLForOBDirect(processId, startDateStr, startHour, startMinute, endDateStr, endHour, endMinute) {
        const warehouseId = getWarehouseId();
        const startDate = startDateStr.replace(/-/g, '%2F');
        const endDate = endDateStr.replace(/-/g, '%2F');
        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&maxIntradayDays=1&spanType=Intraday&startDateIntraday=${startDate}&startHourIntraday=${startHour}&startMinuteIntraday=${startMinute}&endDateIntraday=${endDate}&endHourIntraday=${endHour}&endMinuteIntraday=${endMinute}`;
    }

    function buildDailyFunctionRollupURLForOBDirect(processId, dateStr) {
        const warehouseId = getWarehouseId();
        const date = dateStr.replace(/-/g, '%2F');
        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&spanType=Day&startDateDay=${date}`;
    }

    function buildWeeklyFunctionRollupURLForOBDirect(processId, weekStartDateStr) {
        const warehouseId = getWarehouseId();
        const weekStartDate = weekStartDateStr.replace(/-/g, '%2F');
        // For Week mode, we need spanType=Week and startDateWeek parameter
        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&spanType=Week&startDateWeek=${weekStartDate}&maxIntradayDays=1&startHourIntraday=0&startMinuteIntraday=0&endHourIntraday=0&endMinuteIntraday=0`;
    }

    // Idle time functions removed in Shift Pulse

    // Cache for employee roster data (new hire info)
    let employeeRosterCache = null;
    let rosterFetchPromise = null;

    // Build employee roster URL
    function buildEmployeeRosterURL() {
        const warehouseId = getWarehouseId();
        return `https://fclm-portal.amazon.com/employee/employeeRoster?reportFormat=HTML&warehouseId=${warehouseId}&employeeStatusActive=true&_employeeStatusActive=on&employeeStatusLeaveOfAbsence=true&_employeeStatusLeaveOfAbsence=on&employeeStatusExempt=true&_employeeStatusExempt=on&employeeTypeAmzn=true&_employeeTypeAmzn=on&employeeTypeTemp=true&_employeeTypeTemp=on&employeeType3Pty=true&_employeeType3Pty=on&Employee+ID=Employee+ID&User+ID=User+ID&Employee+Name=Employee+Name&Badge+Barcode+ID=Badge+Barcode+ID&Department+ID=Department+ID&Employment+Start+Date=Employment+Start+Date&Employment+Type=Employment+Type&Employee+Status=Employee+Status&Manager+Name=Manager+Name&Temp+Agency+Code=Temp+Agency+Code&hideColumns=Photo%2CJob+Title%2CManagement+Area+ID%2CShift+Pattern%2CBadge+RFID%2CExempt&submit=true`;
    }

    // Fetch and parse employee roster to get start dates
    function fetchEmployeeRoster() {
        // Return cached data if available
        if (employeeRosterCache) {
            return Promise.resolve(employeeRosterCache);
        }
        // Return existing promise if fetch is in progress
        if (rosterFetchPromise) {
            return rosterFetchPromise;
        }
        
        rosterFetchPromise = new Promise((resolve) => {
            const url = buildEmployeeRosterURL();
            
            GM_xmlhttpRequest({
                method: "GET",
                url: url,
                headers: {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache"
                },
                onload: function(response) {
                    try {
                        const tempHtml = document.createElement('div');
                        tempHtml.innerHTML = response.responseText;
                        
                        const rosterData = {};
                        // Try both with and without tbody
                        let rows = tempHtml.querySelectorAll('table tbody tr');
                        if (rows.length === 0) {
                            rows = tempHtml.querySelectorAll('table tr');
                        }
                        
                        rows.forEach(row => {
                            // Skip header rows
                            if (row.querySelector('th')) return;
                            
                            const cells = row.querySelectorAll('td');
                            if (cells.length >= 6) {
                                // Column 0: Employee ID, Column 5: Employment Start Date
                                const empId = cells[0]?.textContent?.trim();
                                const startDateStr = cells[5]?.textContent?.trim();
                                
                                if (empId && startDateStr) {
                                    // Parse date - try multiple formats
                                    let startDate = null;
                                    
                                    // Format: YYYY/MM/DD (as seen in logs)
                                    if (startDateStr.match(/^\d{4}\/\d{2}\/\d{2}$/)) {
                                        const [year, month, day] = startDateStr.split('/').map(Number);
                                        startDate = new Date(year, month - 1, day);
                                    }
                                    // Format: DD/MM/YYYY (European)
                                    else if (startDateStr.match(/^\d{1,2}\/\d{1,2}\/\d{4}$/)) {
                                        const parts = startDateStr.split('/');
                                        // Try DD/MM/YYYY first (European format)
                                        const day = parseInt(parts[0], 10);
                                        const month = parseInt(parts[1], 10);
                                        const year = parseInt(parts[2], 10);
                                        
                                        // If day > 12, it's definitely DD/MM/YYYY
                                        if (day > 12) {
                                            startDate = new Date(year, month - 1, day);
                                        } else if (month > 12) {
                                            // MM/DD/YYYY format
                                            startDate = new Date(year, day - 1, month);
                                        } else {
                                            // Assume DD/MM/YYYY for European locale (LEJ5 is Germany)
                                            startDate = new Date(year, month - 1, day);
                                        }
                                    }
                                    // Format: YYYY-MM-DD
                                    else if (startDateStr.match(/^\d{4}-\d{2}-\d{2}$/)) {
                                        startDate = new Date(startDateStr);
                                    }
                                    
                                    if (startDate && !isNaN(startDate.getTime())) {
                                        const isNH = isNewHire(startDate);
                                        rosterData[empId] = {
                                            startDate: startDate,
                                            isNewHire: isNH
                                        };
                                    }
                                }
                            }
                        });
                        
                        employeeRosterCache = rosterData;
                        resolve(rosterData);
                    } catch (error) {
                        console.error('Error parsing employee roster:', error);
                        resolve({});
                    }
                },
                onerror: function(error) {
                    console.error('Error fetching employee roster:', error);
                    resolve({});
                }
            });
        });
        
        return rosterFetchPromise;
    }

    // Check if employee is a new hire (< 6 weeks)
    function isNewHire(startDate) {
        const today = new Date();
        const sixWeeksAgo = new Date(today.getTime() - (6 * 7 * 24 * 60 * 60 * 1000));
        return startDate > sixWeeksAgo;
    }

    function parseFunctionRollupForManagerSummary(html, allowedFunctions) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const functionTables = doc.querySelectorAll('table[id^="function-"]');
        
        const results = [];
        
        for (const table of functionTables) {
            const caption = table.querySelector('caption');
            if (!caption) continue;
            
            const captionText = caption.textContent.trim();
            const functionName = captionText.split('[')[0].trim();
            
            // Check if this function is in our allowed list (case-insensitive partial match)
            // If allowedFunctions is empty, allow all functions
            const isAllowed = allowedFunctions.length === 0 || allowedFunctions.some(allowed => 
                functionName.toLowerCase().includes(allowed.toLowerCase()) ||
                allowed.toLowerCase().includes(functionName.toLowerCase())
            );
            
            if (!isAllowed) continue;
            
            const tbody = table.querySelector('tbody');
            if (!tbody) continue;
            
            // Parse employee rows
            const rows = tbody.querySelectorAll('tr.empl-all');
            for (const row of rows) {
                if (row.classList.contains('total')) continue;
                
                const cells = row.querySelectorAll('td');
                if (cells.length < 10) continue;
                
                // Get employee ID from column 1 (check for Anonymous ID 000000)
                const employeeIdCell = cells[1];
                const employeeIdText = employeeIdCell?.textContent.trim() || '';
                if (employeeIdText === '000000') continue; // Skip Anonymous employee
                
                // Get employee name from column 2
                const employeeCell = cells[2];
                const employeeName = employeeCell?.textContent.trim() || 'Unknown';
                if (employeeName.toLowerCase() === 'anonymous') continue; // Also skip by name
                
                // Get manager from column 3
                const managerCell = cells[3];
                const manager = managerCell?.textContent.trim() || 'No Manager';
                
                // Find all cells with class "size-total highlighted"
                const sizeTotalHighlighted = row.querySelectorAll('td.size-total.highlighted');
                
                // Hours: first size-total highlighted cell WITHOUT numeric class (Paid Hours Total)
                // Volume: first size-total highlighted cell WITH numeric class (EACH-Total UNIT)
                let hours = 0;
                let volume = 0;
                
                for (const cell of sizeTotalHighlighted) {
                    const val = normalizeNumber(cell.textContent.trim());
                    
                    if (cell.classList.contains('numeric')) {
                        // This is volume (EACH-Total UNIT) - take the first one (UNIT, not UPH)
                        if (volume === 0) {
                            volume = Math.round(val);
                        }
                    } else {
                        // This is hours (Paid Hours Total)
                        hours = val;
                    }
                }
                
                results.push({
                    functionName,
                    employeeId: employeeIdText,
                    employeeName,
                    manager,
                    hours,
                    volume
                });
            }
        }
        
        return results;
    }

    function aggregateByManager(data) {
        const managerMap = {};
        
        for (const item of data) {
            const key = `${item.functionName}|||${item.manager}`;
            if (!managerMap[key]) {
                managerMap[key] = {
                    functionName: item.functionName,
                    manager: item.manager,
                    hours: 0,
                    volume: 0,
                    employees: []
                };
            }
            managerMap[key].hours += item.hours;
            managerMap[key].volume += item.volume;
            // Store individual employee data
            managerMap[key].employees.push({
                id: item.employeeId,
                name: item.employeeName,
                hours: item.hours,
                volume: item.volume,
                tph: item.hours > 0 ? (item.volume / item.hours) : 0
            });
        }
        
        // Calculate TPH for each manager and sort employees
        const results = Object.values(managerMap).map(item => {
            // Sort employees by hours descending
            item.employees.sort((a, b) => b.hours - a.hours);
            return {
                ...item,
                tph: item.hours > 0 ? Math.round(item.volume / item.hours) : 0
            };
        });
        
        // Sort by function name, then by hours descending
        results.sort((a, b) => {
            if (a.functionName !== b.functionName) {
                return a.functionName.localeCompare(b.functionName);
            }
            return b.hours - a.hours;
        });
        
        return results;
    }

    function groupByFunction(aggregatedData) {
        const grouped = {};
        
        for (const item of aggregatedData) {
            if (!grouped[item.functionName]) {
                grouped[item.functionName] = [];
            }
            grouped[item.functionName].push(item);
        }
        
        return grouped;
    }

    function renderFunctionRollupModal(processName, groupedData, timeRange, fruUrl) {
        const modalBody = document.getElementById('frModalBody');
        const modalTitle = document.getElementById('frModalTitle');
        const modalSubtitle = document.getElementById('frModalSubtitle');
        
        modalTitle.textContent = `${processName} - Manager Performance`;
        modalSubtitle.textContent = timeRange;
        
        if (Object.keys(groupedData).length === 0) {
            modalBody.innerHTML = '<div class="fr-no-data">No data found for the selected functions.</div>';
            return;
        }
        
        let html = '';
        
        // Build FRU link for embedding in header
        const fruLinkHtml = fruUrl ? `<a href="${fruUrl}" target="_blank" class="fr-fru-link-btn" style="float: right;">📋 Open FRU in FCLM</a>` : '';
        
        // Calculate grand totals
        let grandTotalHours = 0;
        let grandTotalVolume = 0;
        const managerTotals = {};
        
        const functionTPHs = {};
        for (const [functionName, managers] of Object.entries(groupedData)) {
            const funcTotalHours = managers.reduce((sum, m) => sum + m.hours, 0);
            const funcTotalVolume = managers.reduce((sum, m) => sum + m.volume, 0);
            functionTPHs[functionName] = funcTotalHours > 0 ? (funcTotalVolume / funcTotalHours) : 0;
        }
        
        for (const [functionName, managers] of Object.entries(groupedData)) {
            const funcTPH = functionTPHs[functionName];
            for (const m of managers) {
                grandTotalHours += m.hours;
                grandTotalVolume += m.volume;
                
                if (!managerTotals[m.manager]) {
                    managerTotals[m.manager] = { hours: 0, volume: 0, employeeCount: 0, weightedTphSum: 0, weightedHoursSum: 0 };
                }
                managerTotals[m.manager].hours += m.hours;
                managerTotals[m.manager].volume += m.volume;
                
                m.employees.forEach(e => {
                    managerTotals[m.manager].employeeCount++;
                    const empTphToFuncAvg = funcTPH > 0 ? (e.tph / funcTPH * 100) : 0;
                    if (e.hours > 0 && funcTPH > 0) {
                        managerTotals[m.manager].weightedTphSum += empTphToFuncAvg * e.hours;
                        managerTotals[m.manager].weightedHoursSum += e.hours;
                    }
                });
            }
        }
        
        const grandTPH = grandTotalHours > 0 ? (grandTotalVolume / grandTotalHours) : 0;
        
        const managerTotalsArray = Object.entries(managerTotals)
            .map(([manager, data]) => {
                const weightedTphToAvg = data.weightedHoursSum > 0 ? (data.weightedTphSum / data.weightedHoursSum) : 0;
                return {
                    manager,
                    volume: Math.round(data.volume),
                    hours: data.hours,
                    pctHours: grandTotalHours > 0 ? (data.hours / grandTotalHours * 100) : 0,
                    tph: data.hours > 0 ? (data.volume / data.hours) : 0,
                    employeeCount: data.employeeCount,
                    weightedTphToAvg: weightedTphToAvg
                };
            })
            .sort((a, b) => b.hours - a.hours);
        
        html += `
            <div class="fr-function-section">
                <div class="fr-function-title" style="background: #232F3E; color: white; border-left-color: #ff9900; display: flex; justify-content: space-between; align-items: center;">📊 Process Total (All Functions, Mix-adjusted) ${fruLinkHtml}</div>
                <div class="fr-process-summary" style="background: #232F3E;">
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Total Volume</span>
                        <span class="fr-process-summary-value" style="color: white;">${Math.round(grandTotalVolume).toLocaleString('en-US')}</span>
                    </div>
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Total Hours</span>
                        <span class="fr-process-summary-value" style="color: white;">${grandTotalHours.toFixed(2)}</span>
                    </div>
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">TPH</span>
                        <span class="fr-process-summary-value" style="color: #ff9900;">${grandTPH.toFixed(1)}</span>
                    </div>
                </div>
                <table class="fr-manager-table fr-fixed-cols">
                    <thead>
                        <tr>
                            <th>Manager</th>
                            <th>HC</th>
                            <th>Volume</th>
                            <th>Hours</th>
                            <th>% of Hours</th>
                            <th>TPH</th>
                            <th>TPH to Avg.</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${managerTotalsArray.map(m => {
                            const tphToAvg = m.weightedTphToAvg;
                            let tphToAvgClass = '';
                            if (m.hours > 0.25) {
                                if (tphToAvg < 85) tphToAvgClass = 'fr-tph-low';
                                else if (tphToAvg > 110) tphToAvgClass = 'fr-tph-high';
                            }
                            return `
                            <tr>
                                <td>${m.manager} <span class="fr-emp-count">(${m.employeeCount})</span></td>
                                <td>${m.employeeCount}</td>
                                <td>${m.volume.toLocaleString('en-US')}</td>
                                <td>${m.hours.toFixed(2)}</td>
                                <td>${m.pctHours.toFixed(1)}%</td>
                                <td>${m.tph.toFixed(1)}</td>
                                <td class="${tphToAvgClass}">${tphToAvg.toFixed(1)}%</td>
                            </tr>
                        `}).join('')}
                    </tbody>
                    <tfoot>
                        <tr>
                            <td>Total</td>
                            <td>${managerTotalsArray.reduce((s, m) => s + m.employeeCount, 0)}</td>
                            <td>${Math.round(grandTotalVolume).toLocaleString('en-US')}</td>
                            <td>${grandTotalHours.toFixed(2)}</td>
                            <td>100.0%</td>
                            <td>${grandTPH.toFixed(1)}</td>
                            <td>100.0%</td>
                        </tr>
                    </tfoot>
                </table>
            </div>
        `;
        
        // Individual function tables (manager-level only)
        for (const [functionName, managers] of Object.entries(groupedData)) {
            const funcTotalHours = managers.reduce((sum, m) => sum + m.hours, 0);
            const funcTotalVolume = managers.reduce((sum, m) => sum + m.volume, 0);
            const funcTPH = funcTotalHours > 0 ? (funcTotalVolume / funcTotalHours) : 0;
            
            const managersWithPct = managers.map(m => ({
                ...m,
                volume: Math.round(m.volume),
                pctHours: funcTotalHours > 0 ? (m.hours / funcTotalHours * 100) : 0,
                tph: m.hours > 0 ? (m.volume / m.hours) : 0,
                employeeCount: m.employees ? m.employees.length : 0
            })).sort((a, b) => b.hours - a.hours);
            
            html += `
                <div class="fr-function-section">
                    <div class="fr-function-title">${functionName}</div>
                    <div class="fr-process-summary">
                        <div class="fr-process-summary-item">
                            <span class="fr-process-summary-label">Total Volume</span>
                            <span class="fr-process-summary-value">${Math.round(funcTotalVolume).toLocaleString('en-US')}</span>
                        </div>
                        <div class="fr-process-summary-item">
                            <span class="fr-process-summary-label">Total Hours</span>
                            <span class="fr-process-summary-value">${funcTotalHours.toFixed(2)}</span>
                        </div>
                        <div class="fr-process-summary-item">
                            <span class="fr-process-summary-label">TPH</span>
                            <span class="fr-process-summary-value">${funcTPH.toFixed(1)}</span>
                        </div>
                    </div>
                    <table class="fr-manager-table fr-fixed-cols">
                        <thead>
                            <tr>
                                <th>Manager</th>
                                <th>HC</th>
                                <th>Volume</th>
                                <th>Hours</th>
                                <th>% of Hours</th>
                                <th>TPH</th>
                                <th>TPH to Avg.</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${managersWithPct.map(m => {
                                const tphToAvg = funcTPH > 0 ? (m.tph / funcTPH * 100) : 0;
                                let tphToAvgClass = '';
                                if (m.hours > 0.25) {
                                    if (tphToAvg < 85) tphToAvgClass = 'fr-tph-low';
                                    else if (tphToAvg > 110) tphToAvgClass = 'fr-tph-high';
                                }
                                return `
                                <tr>
                                    <td>${m.manager} <span class="fr-emp-count">(${m.employeeCount})</span></td>
                                    <td>${m.employeeCount}</td>
                                    <td>${m.volume.toLocaleString('en-US')}</td>
                                    <td>${m.hours.toFixed(2)}</td>
                                    <td>${m.pctHours.toFixed(1)}%</td>
                                    <td>${m.tph.toFixed(1)}</td>
                                    <td class="${tphToAvgClass}">${tphToAvg.toFixed(1)}%</td>
                                </tr>
                            `}).join('')}
                        </tbody>
                        <tfoot>
                            <tr>
                                <td>Function Total</td>
                                <td>${managersWithPct.reduce((s, m) => s + m.employeeCount, 0)}</td>
                                <td>${Math.round(funcTotalVolume).toLocaleString('en-US')}</td>
                                <td>${funcTotalHours.toFixed(2)}</td>
                                <td>100.0%</td>
                                <td>${funcTPH.toFixed(1)}</td>
                                <td>100.0%</td>
                            </tr>
                        </tfoot>
                    </table>
                </div>
            `;
        }
        
        modalBody.innerHTML = html;
    }


    function showFunctionRollupModal() {
        const overlay = document.getElementById('frModalOverlay');
        overlay.classList.add('active');
        
        // Reset to loading state
        document.getElementById('frModalBody').innerHTML = `
            <div class="fr-modal-loading">
                <div class="spinner"></div>
                <div>Loading Function Rollup data...</div>
            </div>
        `;
    }

    function hideFunctionRollupModal() {
        const overlay = document.getElementById('frModalOverlay');
        overlay.classList.remove('active');
    }

    async function fetchAndShowFunctionRollupModal(processKey, timeParams, isTotal = false) {
        // Check both OB and IB process configs
        const processConfig = OB_DIRECT_PROCESSES[processKey] || IB_PROCESSES[processKey] || TSO_DIRECT_PROCESSES[processKey] || VRET_DIRECT_PROCESSES[processKey];
        if (!processConfig) {
            console.error('Unknown process key:', processKey);
            return;
        }
        
        showFunctionRollupModal();
        
        try {
            let url;
            let timeRange;
            
            if (isTotal) {
                // For total row, use the full day or full time range
                const params = getFormParameters();
                
                if (!params) {
                    throw new Error('Could not get form parameters. Please ensure FC code is set.');
                }
                
                // Get date from form inputs as fallback
                const startDateDay = document.getElementById('startDateDay')?.value;
                const startDateIntraday = document.getElementById('startDateIntraday')?.value;
                const endDateIntraday = document.getElementById('endDateIntraday')?.value;
                
                const effectiveStartDate = params.startDateStr || startDateDay || startDateIntraday || new Date().toISOString().split('T')[0];
                const effectiveEndDate = params.endDateStr || endDateIntraday || effectiveStartDate;
                
                if (params.spanType === 'Day') {
                    url = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, effectiveStartDate);
                    timeRange = `Full Day: ${effectiveStartDate}`;
                } else if (params.spanType === 'Week') {
                    // Week mode - use weekly URL with startDateWeek
                    const weekStartDate = params.startDateWeek || effectiveStartDate;
                    url = buildWeeklyFunctionRollupURLForOBDirect(processConfig.processId, weekStartDate);
                    timeRange = `Week starting: ${weekStartDate}`;
                } else if (params.spanType === 'Intraday') {
                    const startHour = params.startHourNum !== undefined ? params.startHourNum : (parseInt(document.getElementById('startHourIntraday')?.value) || 0);
                    const startMinute = params.startMinuteNum !== undefined ? params.startMinuteNum : (parseInt(document.getElementById('startMinuteIntraday')?.value) || 0);
                    let endHour = params.endHourNum !== undefined ? params.endHourNum : (parseInt(document.getElementById('endHourIntraday')?.value) || 12);
                    let endMinute = params.endMinuteNum !== undefined ? params.endMinuteNum : (parseInt(document.getElementById('endMinuteIntraday')?.value) || 0);
                    let cappedEndDate = effectiveEndDate;
                    
                    // Cap end time to current hour if it extends into the future
                    const now = new Date();
                    const nowDateStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
                    if (cappedEndDate > nowDateStr) {
                        cappedEndDate = nowDateStr;
                        endHour = now.getHours() + 1;
                        endMinute = 0;
                    } else if (cappedEndDate === nowDateStr && endHour > now.getHours() + 1) {
                        endHour = now.getHours() + 1;
                        endMinute = 0;
                    }
                    
                    url = buildFunctionRollupURLForOBDirect(
                        processConfig.processId,
                        effectiveStartDate,
                        startHour,
                        startMinute,
                        cappedEndDate,
                        endHour,
                        endMinute
                    );
                    timeRange = `${effectiveStartDate} ${String(startHour).padStart(2, '0')}:${String(startMinute).padStart(2, '0')} - ${cappedEndDate} ${String(endHour).padStart(2, '0')}:${String(endMinute).padStart(2, '0')}`;
                } else {
                    // Fallback to daily
                    url = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, effectiveStartDate);
                    timeRange = `Full Day: ${effectiveStartDate}`;
                }
            } else {
                // For specific hour or day
                if (!timeParams || !timeParams.startDateStr) {
                    throw new Error('Time parameters are missing for hourly view.');
                }
                
                if (timeParams.isFullDay) {
                    // Day mode within Week view - full day view for that specific day
                    url = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, timeParams.startDateStr);
                    timeRange = `Full Day: ${timeParams.startDateStr}`;
                } else {
                    // Intraday mode
                    url = buildFunctionRollupURLForOBDirect(
                        processConfig.processId,
                        timeParams.startDateStr,
                        timeParams.startHour,
                        timeParams.startMinute,
                        timeParams.endDateStr,
                        timeParams.endHour,
                        timeParams.endMinute
                    );
                    timeRange = `${timeParams.startDateStr} ${String(timeParams.startHour).padStart(2, '0')}:${String(timeParams.startMinute).padStart(2, '0')} - ${String(timeParams.endHour).padStart(2, '0')}:${String(timeParams.endMinute).padStart(2, '0')}`;
                }
            }
            
            const html = await fetchURL(url);
            const rawData = parseFunctionRollupForManagerSummary(html, processConfig.functions);
            const aggregated = aggregateByManager(rawData);
            const grouped = groupByFunction(aggregated);
            
            renderFunctionRollupModal(processConfig.name, grouped, timeRange, url);
            
        } catch (error) {
            console.error('Error fetching Function Rollup:', error);
            document.getElementById('frModalBody').innerHTML = `
                <div class="fr-no-data" style="color: #d13212;">
                    Error loading data: ${error.message}
                </div>
            `;
        }
    }

    // ============================================
    // INDIRECT FUNCTION BREAKDOWN MODAL FUNCTIONS
    // ============================================

    // Parse all functions and their hours from Function Rollup HTML
    function parseFunctionHoursFromFRU(html) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const functions = [];
        
        const skipNames = [
            'Function Totals',
            'Summary Small Medium Large HeavyBulky Total',
            'Summary',
            'Total',
            'Totals'
        ];
        
        const tables = doc.querySelectorAll('table');
        
        tables.forEach(table => {
            const caption = table.querySelector('caption');
            if (!caption) return;
            
            const captionText = caption.textContent.trim();
            const functionName = captionText.split('[')[0].trim();
            if (!functionName) return;
            
            if (skipNames.some(skip => functionName.toLowerCase().includes(skip.toLowerCase()))) return;
            if (functionName.toLowerCase().startsWith('summary')) return;
            if (!captionText.includes('[') || !captionText.match(/\[\d+\]/)) return;
            
            const totalRow = table.querySelector('tr.total, tr.empl-all.total');
            let hours = 0;
            
            if (totalRow) {
                const cells = totalRow.querySelectorAll('td');
                // "Total Paid Hours" is the first td.size-total that is NOT .numeric
                for (const cell of cells) {
                    if (cell.classList.contains('size-total') && !cell.classList.contains('numeric')) {
                        const parsed = normalizeNumber(cell.textContent.trim());
                        if (parsed >= 0 && cell.textContent.trim() !== '') {
                            hours = parsed;
                            break; // Take the FIRST match only — that's Total Paid Hours
                        }
                    }
                }
            }
            
            if (hours > 0) {
                functions.push({
                    name: functionName,
                    hours: Math.round(hours * 100) / 100
                });
            }
        });
        
        functions.sort((a, b) => b.hours - a.hours);
        return functions;
    }

    // Fetch and show function breakdown modal for indirect paths
    async function fetchAndShowIndirectFunctionBreakdown(processKey, timeParams, isTotal = false) {
        const processConfig = OB_INDIRECT_PROCESSES[processKey] || IB_INDIRECT_PROCESSES[processKey] || TSO_INDIRECT_PROCESSES[processKey] || VRET_INDIRECT_PROCESSES[processKey];
        if (!processConfig) {
            console.error('Unknown indirect process key:', processKey);
            return;
        }
        
        showFunctionRollupModal();
        
        try {
            if (isTotal) {
                // For total view, fetch hourly breakdown and show matrix view
                await fetchAndShowIndirectHourlyMatrix(processKey, processConfig);
            } else {
                // For single time slot, show simple function breakdown
                let url;
                let timeRange;
                
                if (!timeParams || !timeParams.startDateStr) {
                    throw new Error('Time parameters are missing for hourly view.');
                }
                
                if (timeParams.isFullDay) {
                    url = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, timeParams.startDateStr);
                    timeRange = `Full Day: ${timeParams.startDateStr}`;
                } else {
                    url = buildFunctionRollupURLForOBDirect(
                        processConfig.processId,
                        timeParams.startDateStr,
                        timeParams.startHour,
                        timeParams.startMinute,
                        timeParams.endDateStr,
                        timeParams.endHour,
                        timeParams.endMinute
                    );
                    timeRange = `${timeParams.startDateStr} ${String(timeParams.startHour).padStart(2, '0')}:${String(timeParams.startMinute).padStart(2, '0')} - ${String(timeParams.endHour).padStart(2, '0')}:${String(timeParams.endMinute).padStart(2, '0')}`;
                }
                
                const html = await fetchURL(url);
                const functionData = parseFunctionHoursFromFRU(html);
                
                renderIndirectFunctionBreakdownModal(processConfig.name, functionData, timeRange);
            }
            
        } catch (error) {
            console.error('Error fetching Indirect Function Breakdown:', error);
            document.getElementById('frModalBody').innerHTML = `
                <div class="fr-no-data" style="color: #d13212;">
                    Error loading data: ${error.message}
                </div>
            `;
        }
    }

    // Determine volume type based on process key
    function getVolumeTypeForProcess(processKey) {
        // OB Indirect processes
        if (['shipDock', 'packSupport', 'obLeadPA', 'obProblemSolve', 'pickSupport'].includes(processKey)) {
            return { key: 'obVol', label: 'OB Vol' };
        }
        // IB Indirect processes
        if (['receiveDock', 'receiveSupport', 'tsiSupport', 'stpSupport', 'rsrSupport', 'ibLead'].includes(processKey)) {
            return { key: 'ibVol', label: 'IB Vol' };
        }
        // TSO processes
        if (['transferOut', 'transferOutDock', 'toLeadPA'].includes(processKey)) {
            return { key: 'tsoVol', label: 'TSO Vol' };
        }
        // VRET processes
        if (['vretSupport'].includes(processKey)) {
            return { key: 'vretVol', label: 'VRET Vol' };
        }
        return { key: 'vol', label: 'Volume' };
    }

    // Fetch hourly function breakdown and show matrix view
    async function fetchAndShowIndirectHourlyMatrix(processKey, processConfig) {
        const params = getFormParameters();
        if (!params) {
            throw new Error('Could not get form parameters. Please ensure FC code is set.');
        }
        
        const modalBody = document.getElementById('frModalBody');
        const modalTitle = document.getElementById('frModalTitle');
        const modalSubtitle = document.getElementById('frModalSubtitle');
        
        modalTitle.textContent = `${processConfig.name} - Hourly Function Breakdown`;
        
        // Get the hourly data that's already been fetched (from hourlyData global)
        const volType = getVolumeTypeForProcess(processKey);
        
        // Build time slots based on mode
        const timeSlots = [];
        const startDateDay = document.getElementById('startDateDay')?.value;
        const startDateIntraday = document.getElementById('startDateIntraday')?.value;
        const endDateIntraday = document.getElementById('endDateIntraday')?.value;
        const effectiveStartDate = params.startDateStr || startDateDay || startDateIntraday || new Date().toISOString().split('T')[0];
        
        if (params.spanType === 'Day') {
            // Day mode - hourly slots from 0 to current hour or 23
            const endHour = params.endHourNum !== undefined ? params.endHourNum : 23;
            for (let h = 0; h <= endHour; h++) {
                const endH = h + 1;
                const endHourDisplay = endH === 24 ? '24' : String(endH).padStart(2, '0');
                timeSlots.push({
                    label: `${String(h).padStart(2, '0')}:00-${endHourDisplay}:00`,
                    startDateStr: effectiveStartDate,
                    startHour: h,
                    startMinute: 0,
                    endDateStr: effectiveStartDate,
                    endHour: endH === 24 ? 0 : endH,
                    endMinute: 0
                });
            }
            modalSubtitle.textContent = `Full Day: ${effectiveStartDate}`;
        } else if (params.spanType === 'Week') {
            // Week mode - daily slots
            const weekStartDateStr = params.startDateStr || effectiveStartDate;
            const daysToFetch = params.daysToFetch || 7;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            for (let d = 0; d < daysToFetch; d++) {
                // Parse date parts to avoid UTC timezone issues
                const [y, m, day] = weekStartDateStr.split('-').map(Number);
                const date = new Date(y, m - 1, day + d);
                const dateStr = date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
                const dayName = dayNames[date.getDay()];
                
                timeSlots.push({
                    label: dayName,
                    dateStr: dateStr,
                    startDateStr: dateStr,
                    isFullDay: true
                });
            }
            modalSubtitle.textContent = `Week starting: ${weekStartDateStr}`;
        } else if (params.spanType === 'Intraday') {
            // Intraday mode - hourly slots
            const startHour = params.startHourNum !== undefined ? params.startHourNum : (parseInt(document.getElementById('startHourIntraday')?.value) || 0);
            const endHour = params.endHourNum !== undefined ? params.endHourNum : (parseInt(document.getElementById('endHourIntraday')?.value) || 12);
            const effectiveEndDate = params.endDateStr || endDateIntraday || effectiveStartDate;
            const isCrossMidnight = (effectiveStartDate !== effectiveEndDate) || (startHour > endHour);
            
            if (isCrossMidnight) {
                // Part 1: startHour to 23 on start date
                for (let h = startHour; h <= 23; h++) {
                    const endH = h + 1;
                    const slotEndDate = endH === 24 ? effectiveEndDate : effectiveStartDate;
                    const endHourDisplay = endH === 24 ? '24' : String(endH).padStart(2, '0');
                    timeSlots.push({
                        label: `${String(h).padStart(2, '0')}:00-${endHourDisplay}:00`,
                        startDateStr: effectiveStartDate,
                        startHour: h,
                        startMinute: 0,
                        endDateStr: slotEndDate,
                        endHour: endH === 24 ? 0 : endH,
                        endMinute: 0
                    });
                }
                // Part 2: 0 to endHour on end date
                for (let h = 0; h < endHour; h++) {
                    const endH = h + 1;
                    const endHourDisplay = String(endH).padStart(2, '0');
                    timeSlots.push({
                        label: `${String(h).padStart(2, '0')}:00-${endHourDisplay}:00`,
                        startDateStr: effectiveEndDate,
                        startHour: h,
                        startMinute: 0,
                        endDateStr: effectiveEndDate,
                        endHour: endH,
                        endMinute: 0
                    });
                }
            } else {
                for (let h = startHour; h < endHour; h++) {
                    const endH = h + 1;
                    const endHourDisplay = endH === 24 ? '24' : String(endH).padStart(2, '0');
                    timeSlots.push({
                        label: `${String(h).padStart(2, '0')}:00-${endHourDisplay}:00`,
                        startDateStr: effectiveStartDate,
                        startHour: h,
                        startMinute: 0,
                        endDateStr: effectiveStartDate,
                        endHour: endH === 24 ? 0 : endH,
                        endMinute: 0
                    });
                }
            }
            modalSubtitle.textContent = `${effectiveStartDate} ${String(startHour).padStart(2, '0')}:00 - ${String(endHour).padStart(2, '0')}:00`;
        }
        
        // Filter out future time slots for Intraday mode
        if (params.spanType === 'Intraday') {
            const filtered = filterFutureSlots(timeSlots);
            timeSlots.length = 0;
            filtered.forEach(s => timeSlots.push(s));
        }
        
        if (timeSlots.length === 0) {
            modalBody.innerHTML = '<div class="fr-no-data">No time slots to display.</div>';
            return;
        }
        
        // Show loading with progress
        modalBody.innerHTML = `
            <div class="fr-modal-loading">
                <div class="spinner"></div>
                <div>Loading hourly function data... <span id="frMatrixProgress">0/${timeSlots.length}</span></div>
            </div>
        `;
        
        // Fetch function data for each time slot
        const hourlyFunctionData = [];
        const allFunctions = new Set();
        
        for (let i = 0; i < timeSlots.length; i++) {
            const slot = timeSlots[i];
            document.getElementById('frMatrixProgress').textContent = `${i + 1}/${timeSlots.length}`;
            
            try {
                let url;
                if (slot.isFullDay) {
                    url = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, slot.startDateStr);
                } else {
                    url = buildFunctionRollupURLForOBDirect(
                        processConfig.processId,
                        slot.startDateStr,
                        slot.startHour,
                        slot.startMinute,
                        slot.endDateStr,
                        slot.endHour,
                        slot.endMinute
                    );
                }
                
                const html = await fetchURL(url);
                const functionData = parseFunctionHoursFromFRU(html);
                
                // Get volume from processPathData (global) if available
                let volume = 0;
                const matchingHourData = processPathData.find(hd => {
                    if (slot.isFullDay) {
                        return hd.dateStr === slot.startDateStr || hd.dateStr === slot.dateStr;
                    }
                    // Match by timeRange label (e.g. "11:00-12:00")
                    if (hd.timeRange === slot.label) return true;
                    // Also try matching by hour
                    if (hd.timeRange && hd.timeRange.includes('-')) {
                        const hrStart = parseInt(hd.timeRange.split(':')[0]);
                        if (hrStart === slot.startHour) return true;
                    }
                    return false;
                });
                
                if (matchingHourData) {
                    if (volType.key === 'obVol') {
                        volume = parseVol(matchingHourData.obVol || matchingHourData.obTot?.vol || 0);
                    } else if (volType.key === 'ibVol') {
                        volume = parseVol(matchingHourData.ibTotal?.vol || matchingHourData.ibTot?.vol || 0);
                    } else if (volType.key === 'tsoVol') {
                        volume = parseVol(matchingHourData.daBldgTransferVol || matchingHourData.daBldgTransfer?.vol || matchingHourData.tsoTotSummary?.vol || 0);
                    } else if (volType.key === 'vretVol') {
                        volume = parseVol(matchingHourData.vretTot?.vol || 0);
                    }
                }
                
                // Build function hours map
                const funcHoursMap = {};
                functionData.forEach(f => {
                    funcHoursMap[f.name] = f.hours;
                    allFunctions.add(f.name);
                });
                
                hourlyFunctionData.push({
                    label: slot.label,
                    volume: volume,
                    functions: funcHoursMap
                });
                
            } catch (error) {
                console.warn(`Error fetching data for slot ${slot.label}:`, error);
                hourlyFunctionData.push({
                    label: slot.label,
                    volume: 0,
                    functions: {}
                });
            }
        }
        
        // Sort functions by total hours descending
        const functionTotals = {};
        allFunctions.forEach(fn => {
            functionTotals[fn] = hourlyFunctionData.reduce((sum, hd) => sum + (hd.functions[fn] || 0), 0);
        });
        const sortedFunctions = Array.from(allFunctions).sort((a, b) => functionTotals[b] - functionTotals[a]);
        
        // Build FRU URL for the full time range
        let fruUrl;
        if (params.spanType === 'Day') {
            fruUrl = buildDailyFunctionRollupURLForOBDirect(processConfig.processId, effectiveStartDate);
        } else if (params.spanType === 'Week') {
            const weekStartDate = params.startDateWeek || effectiveStartDate;
            fruUrl = buildWeeklyFunctionRollupURLForOBDirect(processConfig.processId, weekStartDate);
        } else if (params.spanType === 'Intraday') {
            const startHour = params.startHourNum !== undefined ? params.startHourNum : 0;
            const startMinute = params.startMinuteNum !== undefined ? params.startMinuteNum : 0;
            const effectiveEndDate = params.endDateStr || endDateIntraday || effectiveStartDate;
            let endHour = params.endHourNum !== undefined ? params.endHourNum : 12;
            let endMinute = params.endMinuteNum !== undefined ? params.endMinuteNum : 0;
            fruUrl = buildFunctionRollupURLForOBDirect(processConfig.processId, effectiveStartDate, startHour, startMinute, effectiveEndDate, endHour, endMinute);
        }
        
        // Render the matrix
        renderIndirectHourlyMatrixModal(processConfig.name, volType.label, hourlyFunctionData, sortedFunctions, fruUrl);
    }

    // Render the hourly matrix modal
    function renderIndirectHourlyMatrixModal(processName, volLabel, hourlyData, functions, fruUrl) {
        const modalBody = document.getElementById('frModalBody');
        
        if (!hourlyData || hourlyData.length === 0 || functions.length === 0) {
            modalBody.innerHTML = '<div class="fr-no-data">No function data found for the selected time range.</div>';
            return;
        }
        
        // Calculate totals
        const totalVolume = hourlyData.reduce((sum, hd) => sum + hd.volume, 0);
        const functionTotals = {};
        functions.forEach(fn => {
            functionTotals[fn] = hourlyData.reduce((sum, hd) => sum + (hd.functions[fn] || 0), 0);
        });
        const grandTotalHours = Object.values(functionTotals).reduce((sum, h) => sum + h, 0);
        const totalTPH = grandTotalHours > 0 ? totalVolume / grandTotalHours : 0;
        
        const fruLinkHtml = fruUrl ? `<div class="fr-process-summary-item" style="margin-left: auto;"><a href="${fruUrl}" target="_blank" class="fr-fru-link-btn">📋 Open FRU in FCLM</a></div>` : '';
        
        let html = `
            <div class="fr-function-section">
                <div class="fr-process-summary" style="background: #232F3E;">
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Total ${volLabel}</span>
                        <span class="fr-process-summary-value" style="color: white;">${Math.round(totalVolume).toLocaleString('en-US')}</span>
                    </div>
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Total Hours</span>
                        <span class="fr-process-summary-value" style="color: #ff9900;">${grandTotalHours.toFixed(1)}</span>
                    </div>
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Functions</span>
                        <span class="fr-process-summary-value" style="color: white;">${functions.length}</span>
                    </div>
                    ${fruLinkHtml}
                </div>
                <div style="overflow-x: auto;">
                    <table class="fr-manager-table" style="font-size: 11px; border-collapse: collapse;">
                        <thead>
                            <tr>
                                <th style="position: sticky; left: 0; background: #f8f8f8; z-index: 1; border-right: 3px solid #146eb4;">Time</th>
                                <th style="text-align: right; border-right: 1px solid #ccc;">${volLabel}</th>
                                <th style="text-align: center; border-right: 1px solid #ccc;">TPH</th>
                                <th style="text-align: right; border-right: 3px solid #146eb4;">HC</th>
                                ${functions.map((fn, i) => {
                                    const isLast = i === functions.length - 1;
                                    const borderStyle = isLast ? '' : 'border-right: 1px solid #ccc;';
                                    return `<th style="text-align: right; min-width: 70px; max-width: 90px; white-space: normal; word-wrap: break-word; font-size: 10px; line-height: 1.2; ${borderStyle}" title="${fn}">${fn}</th>`;
                                }).join('')}
                            </tr>
                        </thead>
                        <tbody>
                            ${hourlyData.map(hd => {
                                const rowTotalHours = functions.reduce((sum, fn) => sum + (hd.functions[fn] || 0), 0);
                                const rowTPH = rowTotalHours > 0 ? hd.volume / rowTotalHours : 0;
                                return `
                                <tr>
                                    <td style="position: sticky; left: 0; background: white; z-index: 1; font-weight: 500; border-right: 3px solid #146eb4;">${hd.label}</td>
                                    <td style="text-align: right; font-family: monospace; border-right: 1px solid #ccc;">${Math.round(hd.volume).toLocaleString('en-US')}</td>
                                    <td style="text-align: center; font-family: monospace; border-right: 1px solid #ccc;">${hd.volume > 0 && rowTotalHours > 0 ? rowTPH.toFixed(1) : '-'}</td>
                                    <td style="text-align: right; font-family: monospace; border-right: 3px solid #146eb4;">${rowTotalHours > 0 ? rowTotalHours.toFixed(1) : '-'}</td>
                                    ${functions.map((fn, i) => {
                                        const hrs = hd.functions[fn] || 0;
                                        const isLast = i === functions.length - 1;
                                        const borderStyle = isLast ? '' : 'border-right: 1px solid #ccc;';
                                        return `<td style="text-align: right; font-family: monospace; ${borderStyle}">${hrs > 0 ? hrs.toFixed(1) : '-'}</td>`;
                                    }).join('')}
                                </tr>
                            `}).join('')}
                        </tbody>
                        <tfoot>
                            <tr>
                                <td style="position: sticky; left: 0; background: #232F3E; z-index: 1; border-right: 3px solid #146eb4;"><strong>Total</strong></td>
                                <td style="text-align: right; border-right: 1px solid #ccc;"><strong>${Math.round(totalVolume).toLocaleString('en-US')}</strong></td>
                                <td style="text-align: center; border-right: 1px solid #ccc;"><strong>${totalTPH > 0 ? totalTPH.toFixed(1) : '-'}</strong></td>
                                <td style="text-align: right; border-right: 3px solid #146eb4;"><strong>${grandTotalHours.toFixed(1)}</strong></td>
                                ${functions.map((fn, i) => {
                                    const isLast = i === functions.length - 1;
                                    const borderStyle = isLast ? '' : 'border-right: 1px solid #ccc;';
                                    return `<td style="text-align: right; ${borderStyle}"><strong>${functionTotals[fn].toFixed(1)}</strong></td>`;
                                }).join('')}
                            </tr>
                        </tfoot>
                    </table>
                </div>
            </div>
        `;
        
        modalBody.innerHTML = html;
    }

    // Render the function breakdown modal for indirect paths
    function renderIndirectFunctionBreakdownModal(processName, functionData, timeRange) {
        const modalBody = document.getElementById('frModalBody');
        const modalTitle = document.getElementById('frModalTitle');
        const modalSubtitle = document.getElementById('frModalSubtitle');
        
        modalTitle.textContent = `${processName} - Function Breakdown`;
        modalSubtitle.textContent = timeRange;
        
        if (!functionData || functionData.length === 0) {
            modalBody.innerHTML = '<div class="fr-no-data">No function data found for the selected time range.</div>';
            return;
        }
        
        const totalHours = functionData.reduce((sum, f) => sum + f.hours, 0);
        
        let html = `
            <div class="fr-function-section">
                <div class="fr-process-summary" style="background: #232F3E;">
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Total Hours</span>
                        <span class="fr-process-summary-value" style="color: #ff9900;">${totalHours.toFixed(1)}</span>
                    </div>
                    <div class="fr-process-summary-item">
                        <span class="fr-process-summary-label" style="color: #aaa;">Functions</span>
                        <span class="fr-process-summary-value" style="color: white;">${functionData.length}</span>
                    </div>
                </div>
                <table class="fr-manager-table">
                    <thead>
                        <tr>
                            <th style="width: 60%;">Function</th>
                            <th style="text-align: right;">Hours</th>
                            <th style="text-align: right;">% of Total</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${functionData.map(f => {
                            const pctOfTotal = totalHours > 0 ? (f.hours / totalHours * 100) : 0;
                            return `
                            <tr>
                                <td>${f.name}</td>
                                <td style="text-align: right; font-family: monospace;">${f.hours.toFixed(1)}</td>
                                <td style="text-align: right; font-family: monospace;">${pctOfTotal.toFixed(1)}%</td>
                            </tr>
                        `}).join('')}
                    </tbody>
                    <tfoot>
                        <tr>
                            <td><strong>Total</strong></td>
                            <td style="text-align: right;"><strong>${totalHours.toFixed(1)}</strong></td>
                            <td style="text-align: right;"><strong>100.0%</strong></td>
                        </tr>
                    </tfoot>
                </table>
            </div>
        `;
        
        modalBody.innerHTML = html;
    }

    // Render clickable cell for indirect process paths
    function renderClickableIndirectProcessCell(process, vol, manualTargetValue, processKey, timeParams, isWeekMode) {
        const actTPH = formatRate(process.rate);
        const pprPlanRate = normalizeNumber(process.planRate);
        const manualRate = normalizeNumber(manualTargetValue);
        
        const useManualOverride = manualRate > 0;
        const planRateRaw = useManualOverride ? manualTargetValue : (pprPlanRate > 0 ? process.planRate : '0');
        const planRate = formatRate(planRateRaw);
        const actHrs = isWeekMode ? Math.round(normalizeNumber(process.hrs)) : formatHrsRound(process.hrs);
        
        let varHrs;
        if (useManualOverride) {
            const processVol = parseVol(process.vol || vol);
            const actHrsNum = normalizeNumber(process.hrs);
            const targetHrs = manualRate > 0 ? processVol / manualRate : 0;
            const varHrsNum = targetHrs - actHrsNum;
            varHrs = formatVarHrs(varHrsNum, false);
        } else {
            varHrs = formatVarHrs(process.varHrs, false);
        }
        
        const timeParamsJson = JSON.stringify(timeParams).replace(/"/g, '&quot;');
        // Make the hours cell clickable for function breakdown
        const hrsCell = `<td><span class="fr-indirect-link" data-process="${processKey}" data-time-params="${timeParamsJson}" style="cursor: pointer; color: #0066c0; text-decoration: underline;">${actHrs}</span></td>`;
        return `<td>${actTPH}</td><td>${planRate}</td>${hrsCell}<td>${varHrs}</td>`;
    }

    // Initialize modal close handlers
    function initFunctionRollupModal() {
        const overlay = document.getElementById('frModalOverlay');
        const closeBtn = document.getElementById('frModalClose');
        
        if (closeBtn) {
            closeBtn.addEventListener('click', hideFunctionRollupModal);
        }
        
        if (overlay) {
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) {
                    hideFunctionRollupModal();
                }
            });
        }
        
        // Close on Escape key
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                hideFunctionRollupModal();
            }
        });
    }

    function getFormParameters() {
        const spanType = document.getElementById('spanType')?.value || 'Day';
        const warehouseId = getWarehouseId();

        if (!warehouseId) {
            console.error('❌ Cannot get form parameters: No FC code');
            return null;
        }

        const params = {
            warehouseId: warehouseId,
            spanType: spanType
        };

        if (spanType === 'Day') {
            const dayInput = document.getElementById('startDateDay')?.value;
            if (dayInput) {
                const dateStr = dayInput;
                params.startDateDay = dateStr.replace(/-/g, '/');
                params.startDateStr = dateStr;

                const selectedDate = new Date(dateStr);
                const today = new Date();
                today.setHours(0, 0, 0, 0);
                selectedDate.setHours(0, 0, 0, 0);

                if (selectedDate.getTime() === today.getTime()) {
                    params.endHourNum = new Date().getHours();
                    params.startHourNum = 0;
                    currentHour = new Date().getHours();
                } else if (selectedDate.getTime() < today.getTime()) {
                    params.startHourNum = 0;
                    params.endHourNum = 23;
                    currentHour = 24;
                } else {
                    params.startHourNum = 0;
                    params.endHourNum = -1;
                    currentHour = -1;
                }
            }
        } else if (spanType === 'Week') {
            const weekInput = document.getElementById('startDateWeek')?.value;
            if (weekInput) {
                params.startDateWeek = weekInput.replace(/-/g, '/');
                params.startDateStr = weekInput;
                
                // Parse date parts to avoid UTC timezone issues
                const dateParts = weekInput.split('-');
                const startYear = parseInt(dateParts[0]);
                const startMonth = parseInt(dateParts[1]) - 1;
                const startDay = parseInt(dateParts[2]);
                
                // Calculate end of week (Sunday + 6 days)
                const weekStart = new Date(startYear, startMonth, startDay);
                const weekEnd = new Date(startYear, startMonth, startDay + 6);
                params.endDateStr = weekEnd.getFullYear() + '-' + String(weekEnd.getMonth() + 1).padStart(2, '0') + '-' + String(weekEnd.getDate()).padStart(2, '0');
                
                // Determine how many days to fetch based on current date
                const today = new Date();
                today.setHours(0, 0, 0, 0);
                
                if (weekEnd < today) {
                    // Past week - fetch all 7 days
                    params.daysToFetch = 7;
                } else if (weekStart > today) {
                    // Future week - no data
                    params.daysToFetch = 0;
                } else {
                    // Current week - fetch up to today
                    // Use date-based diff to avoid DST issues with millisecond math
                    const daysDiff = Math.round((today - weekStart) / (1000 * 60 * 60 * 24));
                    params.daysToFetch = daysDiff + 1; // Include today
                }
            }
        } else if (spanType === 'Intraday') {
            const startDate = document.getElementById('startDateIntraday')?.value;
            const startHour = document.getElementById('startHourIntraday')?.value || '0';
            const startMinute = document.getElementById('startMinuteIntraday')?.value || '0';
            const endDate = document.getElementById('endDateIntraday')?.value;
            const endHour = document.getElementById('endHourIntraday')?.value || '12';
            const endMinute = document.getElementById('endMinuteIntraday')?.value || '0';

            if (startDate && endDate) {
                params.startDateIntraday = startDate.replace(/-/g, '/');
                params.startHourIntraday = startHour;
                params.startMinuteIntraday = startMinute;
                params.endDateIntraday = endDate.replace(/-/g, '/');
                params.endHourIntraday = endHour;
                params.endMinuteIntraday = endMinute;
                params.startHourNum = parseInt(startHour);
                params.startMinuteNum = parseInt(startMinute);
                params.endHourNum = parseInt(endHour);
                params.endMinuteNum = parseInt(endMinute);
                params.startDateStr = startDate;
                params.endDateStr = endDate;
                currentHour = parseInt(endHour);
            }
        }

        return params;
    }

    function buildSummaryURL(processId) {
        const params = getFormParameters();
        if (!params) return null;

        let url = `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${params.warehouseId}&processId=${processId}&spanType=${params.spanType}`;

        if (params.spanType === 'Intraday') {
            url += `&startDateIntraday=${encodeURIComponent(params.startDateIntraday)}`;
            url += `&startHourIntraday=${params.startHourIntraday}`;
            url += `&startMinuteIntraday=${params.startMinuteIntraday}`;
            url += `&endDateIntraday=${encodeURIComponent(params.endDateIntraday)}`;
            url += `&endHourIntraday=${params.endHourIntraday}`;
            url += `&endMinuteIntraday=${params.endMinuteIntraday}`;
        } else if (params.spanType === 'Week') {
            url += `&startDateWeek=${encodeURIComponent(params.startDateWeek)}`;
        } else if (params.spanType === 'Day') {
            url += `&startDateDay=${encodeURIComponent(params.startDateDay)}`;
        }

        return url;
    }

    function buildHourlyURL(processId, startDateStr, startHour, endDateStr, endHour) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return null;

        const startDate = startDateStr.replace(/-/g, '/');
        const endDate = endDateStr.replace(/-/g, '/');

        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&spanType=Intraday&startDateIntraday=${encodeURIComponent(startDate)}&startHourIntraday=${startHour}&startMinuteIntraday=0&endDateIntraday=${encodeURIComponent(endDate)}&endHourIntraday=${endHour}&endMinuteIntraday=0`;
    }

    function buildHourlyURLWithMinutes(processId, startDateStr, startHour, startMinute, endDateStr, endHour, endMinute) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return null;

        const startDate = startDateStr.replace(/-/g, '/');
        const endDate = endDateStr.replace(/-/g, '/');

        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&spanType=Intraday&startDateIntraday=${encodeURIComponent(startDate)}&startHourIntraday=${startHour}&startMinuteIntraday=${startMinute}&endDateIntraday=${encodeURIComponent(endDate)}&endHourIntraday=${endHour}&endMinuteIntraday=${endMinute}`;
    }

    function buildHourlyProcessPathURLWithMinutes(startDateStr, startHour, startMinute, endDateStr, endHour, endMinute) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return null;

        const startDate = startDateStr.replace(/-/g, '/');
        const endDate = endDateStr.replace(/-/g, '/');

        return `https://fclm-portal.amazon.com/reports/processPathRollup?warehouseId=${warehouseId}&spanType=Intraday&startDateIntraday=${encodeURIComponent(startDate)}&startHourIntraday=${startHour}&startMinuteIntraday=${startMinute}&endDateIntraday=${encodeURIComponent(endDate)}&endHourIntraday=${endHour}&endMinuteIntraday=${endMinute}`;
    }

    // Build URL for daily data in Week mode (using Day spanType for each day)
    function buildDailyURL(processId, dateStr) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return null;

        const date = dateStr.replace(/-/g, '/');
        return `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${warehouseId}&processId=${processId}&spanType=Day&startDateDay=${encodeURIComponent(date)}`;
    }

    // Build URL for daily process path data in Week mode
    function buildDailyProcessPathURL(dateStr) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return null;

        const date = dateStr.replace(/-/g, '/');
        return `https://fclm-portal.amazon.com/reports/processPathRollup?warehouseId=${warehouseId}&spanType=Day&startDateDay=${encodeURIComponent(date)}`;
    }

    // Build clickable PPR link for Week mode (day name links to that day's PPR)
    function buildPPRDayLink(dayName, dateStr) {
        const warehouseId = getWarehouseId();
        if (!warehouseId) return dayName;

        const date = dateStr.replace(/-/g, '/');
        const url = `https://fclm-portal.amazon.com/reports/processPathRollup?warehouseId=${warehouseId}&spanType=Day&startDateDay=${encodeURIComponent(date)}`;
        return `<a href="${url}" target="_blank" style="color: #0066c0; text-decoration: none; font-weight: 500;">${dayName}</a>`;
    }

    // Helper function to build a clickable PPR link for the Time column
    // Handles both hourly time ranges (e.g., "08:00-09:00") and day names (e.g., "Mon")
    function buildPPRTimeLink(timeRange, dateStr) {
        const params = getFormParameters();
        if (!params || !params.startDateStr) return timeRange;

        // Check if this is a day name (Week mode) - day names are 3 chars without ':'
        const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
        if (dayNames.includes(timeRange)) {
            // Week mode - use the dateStr passed from the data
            if (dateStr) {
                return buildPPRDayLink(timeRange, dateStr);
            }
            return timeRange;
        }

        // Hourly mode - parse time range
        const [startTime, endTime] = timeRange.split('-');
        if (!startTime || !endTime) return timeRange;
        
        const [startHourStr, startMinStr] = startTime.split(':');
        const [endHourStr, endMinStr] = endTime.split(':');
        const startHour = parseInt(startHourStr);
        const startMinute = parseInt(startMinStr) || 0;
        let endHour = parseInt(endHourStr);
        let endMinute = parseInt(endMinStr) || 0;
        
        // Use the slot's own date if provided, otherwise fall back to form params
        let startDateStrLocal = dateStr || params.startDateStr;
        let endDateStrLocal = startDateStrLocal;
        
        // Handle midnight crossing (24:00 means next day 00:00)
        if (endHour === 24) {
            endHour = 0;
            endMinute = 0;
            const nextDay = new Date(startDateStrLocal);
            nextDay.setDate(nextDay.getDate() + 1);
            endDateStrLocal = nextDay.toISOString().split('T')[0];
        }

        const url = buildHourlyProcessPathURLWithMinutes(startDateStrLocal, startHour, startMinute, endDateStrLocal, endHour, endMinute);
        if (!url) return timeRange;

        return `<a href="${url}" target="_blank" style="color: #0066c0; text-decoration: none; font-weight: 500;">${timeRange}</a>`;
    }

    function buildEmployeeTimeDetailsURL(employeeId) {
        const params = getFormParameters();
        if (!params) return null;

        let url = `https://fclm-portal.amazon.com/employee/timeDetails?employeeId=${employeeId}&warehouseId=${params.warehouseId}&spanType=${params.spanType}`;

        if (params.spanType === 'Day') {
            url += `&startDateDay=${encodeURIComponent(params.startDateDay)}`;
        } else if (params.spanType === 'Week') {
            url += `&startDateWeek=${encodeURIComponent(params.startDateWeek)}`;
        } else if (params.spanType === 'Intraday') {
            url += `&startDateIntraday=${encodeURIComponent(params.startDateIntraday)}`;
            url += `&startHourIntraday=${params.startHourIntraday}`;
            url += `&startMinuteIntraday=${params.startMinuteIntraday}`;
            url += `&endDateIntraday=${encodeURIComponent(params.endDateIntraday)}`;
            url += `&endHourIntraday=${params.endHourIntraday}`;
            url += `&endMinuteIntraday=${params.endMinuteIntraday}`;
        }

        return url;
    }

    function buildTimeOnTaskURL() {
        const params = getFormParameters();
        if (!params) return null;

        let url = `https://fclm-portal.amazon.com/reports/timeOnTask?reportFormat=HTML&warehouseId=${params.warehouseId}`;

        if (params.spanType === 'Day' && params.startDateDay) {
            url += `&spanType=Day`;
            url += `&startDateDay=${encodeURIComponent(params.startDateDay)}`;
        } else if (params.spanType === 'Week' && params.startDateWeek) {
            url += `&spanType=Week`;
            url += `&startDateWeek=${encodeURIComponent(params.startDateWeek)}`;
        } else if (params.spanType === 'Intraday') {
            url += `&spanType=Intraday`;
            url += `&startDateIntraday=${encodeURIComponent(params.startDateIntraday)}`;
            url += `&startHourIntraday=${params.startHourIntraday}`;
            url += `&startMinuteIntraday=${params.startMinuteIntraday}`;
            url += `&endDateIntraday=${encodeURIComponent(params.endDateIntraday)}`;
            url += `&endHourIntraday=${params.endHourIntraday}`;
            url += `&endMinuteIntraday=${params.endMinuteIntraday}`;
        }

        return url;
    }

    function buildTimeDetailsURL(employeeId) {
        const params = getFormParameters();
        if (!params) return null;

        let url = `https://fclm-portal.amazon.com/employee/timeDetails?employeeId=${employeeId}&warehouseId=${params.warehouseId}&spanType=${params.spanType}`;

        if (params.spanType === 'Day' && params.startDateDay) {
            url += `&startDateDay=${encodeURIComponent(params.startDateDay)}`;
        } else if (params.spanType === 'Week' && params.startDateWeek) {
            url += `&startDateWeek=${encodeURIComponent(params.startDateWeek)}`;
        } else if (params.spanType === 'Intraday') {
            url += `&startDateIntraday=${encodeURIComponent(params.startDateIntraday)}`;
            url += `&startHourIntraday=${params.startHourIntraday}`;
            url += `&startMinuteIntraday=${params.startMinuteIntraday}`;
            url += `&endDateIntraday=${encodeURIComponent(params.endDateIntraday)}`;
            url += `&endHourIntraday=${params.endHourIntraday}`;
            url += `&endMinuteIntraday=${params.endMinuteIntraday}`;
        }

        return url;
    }

    function buildTimeDetailsURLForDay(employeeId, dateStr) {
        const params = getFormParameters();
        if (!params) return null;

        const dateFormatted = dateStr.replace(/-/g, '/');
        return `https://fclm-portal.amazon.com/employee/timeDetails?employeeId=${employeeId}&warehouseId=${params.warehouseId}&spanType=Day&startDateDay=${encodeURIComponent(dateFormatted)}`;
    }

    function parseTimeOnTaskData(html) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        
        const employees = [];
        const rows = doc.querySelectorAll('tr.tot-row');
        
        rows.forEach(row => {
            const cells = row.querySelectorAll('td');
            if (cells.length < 6) return;
            
            const employeeIdLink = cells[0]?.querySelector('a');
            const employeeId = employeeIdLink?.textContent.trim() || '';
            const employeeName = cells[1]?.textContent.trim() || '';
            const manager = cells[2]?.textContent.trim() || 'No Manager';
            const timeOnTask = normalizeNumber(cells[3]?.textContent.trim());
            const totalTime = normalizeNumber(cells[4]?.textContent.trim());
            const pctTimeOnTask = normalizeNumber(cells[5]?.textContent.trim());
            
            if (pctTimeOnTask < 100 && totalTime > 0) {
                const timeOffTask = totalTime - timeOnTask;
                employees.push({
                    employeeId,
                    employeeName,
                    manager,
                    timeOnTask,
                    totalTime,
                    pctTimeOnTask,
                    timeOffTask
                });
            }
        });
        
        const managerMap = new Map();
        employees.forEach(emp => {
            if (!managerMap.has(emp.manager)) {
                managerMap.set(emp.manager, {
                    manager: emp.manager,
                    totalTimeOffTask: 0,
                    employeeCount: 0,
                    employees: []
                });
            }
            const mgrData = managerMap.get(emp.manager);
            mgrData.totalTimeOffTask += emp.timeOffTask;
            mgrData.employeeCount++;
            mgrData.employees.push(emp);
        });
        
        const managerSummary = Array.from(managerMap.values())
            .sort((a, b) => b.totalTimeOffTask - a.totalTimeOffTask);
        
        return { employees, managerSummary };
    }

    async function fetchTimeOnTaskData() {
        const params = getFormParameters();
        
        if (params?.spanType === 'Week') {
            const daySlots = generateDaySlots(params);
            if (daySlots.length === 0) return null;

            const dayPromises = daySlots.map(async (dayData) => {
                const dateFormatted = dayData.dateStr.replace(/-/g, '/');
                const url = `https://fclm-portal.amazon.com/reports/timeOnTask?reportFormat=HTML&warehouseId=${params.warehouseId}&spanType=Day&startDateDay=${encodeURIComponent(dateFormatted)}`;
                
                try {
                    const html = await fetchURL(url);
                    currentFetchStep++;
                    updateLoadingBar();
                    const data = parseTimeOnTaskData(html);
                    return { dayName: dayData.dayName, dateStr: dayData.dateStr, dayIndex: dayData.dayIndex, data: data };
                } catch (error) {
                    console.error(`❌ Error fetching Time Off Task for ${dayData.dayName}:`, error);
                    failedSlots.push(`TOT ${dayData.dayName}`);
                    currentFetchStep++;
                    updateLoadingBar();
                    return null;
                }
            });

            const results = await Promise.all(dayPromises);
            
            const fullDayNames = { 'Sun': 'Sunday', 'Mon': 'Monday', 'Tue': 'Tuesday', 'Wed': 'Wednesday', 'Thu': 'Thursday', 'Fri': 'Friday', 'Sat': 'Saturday' };
            
            const dayData = [];
            let totalTimeOffTask = 0;
            
            results.forEach(result => {
                if (!result || !result.data) return;
                let dayTotal = 0;
                result.data.managerSummary.forEach(mgr => { dayTotal += mgr.totalTimeOffTask; });
                totalTimeOffTask += dayTotal;
                dayData.push({
                    dayName: fullDayNames[result.dayName] || result.dayName,
                    dateStr: result.dateStr,
                    dayIndex: result.dayIndex,
                    totalTimeOffTask: dayTotal,
                    employeeCount: result.data.employees.length,
                    managerSummary: result.data.managerSummary
                });
            });
            
            dayData.sort((a, b) => a.dayIndex - b.dayIndex);
            timeOffTaskData = { dayData, totalTimeOffTask, isWeekMode: true };
            return timeOffTaskData;
        }

        const url = buildTimeOnTaskURL();
        if (!url) return null;

        try {
            const html = await fetchURL(url);
            currentFetchStep++;
            updateLoadingBar();
            const data = parseTimeOnTaskData(html);
            data.isWeekMode = false;
            timeOffTaskData = data;
            return data;
        } catch (error) {
            console.error('❌ Error fetching Time On Task:', error);
            failedSlots.push('Time On Task');
            currentFetchStep++;
            updateLoadingBar();
            return null;
        }
    }

    function renderTimeOffTaskTable(data) {
        if (!data) data = timeOffTaskData;
        
        if (data?.isWeekMode) {
            if (!data.dayData) { console.warn('⚠️ No Time Off Task day data to render'); return; }
        } else if (!data || !data.managerSummary) {
            console.warn('⚠️ No Time Off Task data to render'); return;
        }

        const container = document.getElementById('timeOffTaskContainer');
        if (!container) { console.error('❌ timeOffTaskContainer not found'); return; }
        const table = container.querySelector('#timeOffTaskTable');
        if (!table) { console.error('❌ timeOffTaskTable not found'); return; }
        const thead = table.querySelector('thead');
        const tbody = container.querySelector('#timeOffTaskTableBody');
        const tfoot = table.querySelector('tfoot');
        if (!tbody) { console.error('❌ timeOffTaskTableBody not found'); return; }

        if (data.isWeekMode && data.dayData) {
            thead.innerHTML = '<tr><th style="width: 40px;"></th><th>Day / Manager / Employee</th><th>Employees</th><th>Time Off Task (hrs)</th></tr>';
            let html = '';
            data.dayData.forEach((day, dayIdx) => {
                const dayId = `tot-day-${dayIdx}`;
                const dayHours = Math.round(day.totalTimeOffTask);
                let dayClass = '';
                if (day.totalTimeOffTask >= 10) dayClass = 'time-off-task-high';
                else if (day.totalTimeOffTask >= 5) dayClass = 'time-off-task-medium';

                html += `<tr class="tot-day-row" data-day-id="${dayId}" style="background: #e8f4fc; cursor: pointer;">`;
                html += `<td><span class="expand-icon" id="icon-${dayId}">▶</span></td>`;
                html += `<td><strong>${day.dayName}</strong></td>`;
                html += `<td>${day.employeeCount}</td>`;
                html += `<td class="${dayClass}"><strong>${dayHours}</strong></td></tr>`;

                if (day.managerSummary) {
                    day.managerSummary.forEach((mgrData, mgrIdx) => {
                        const managerId = `${dayId}-mgr-${mgrIdx}`;
                        const mgrHours = mgrData.totalTimeOffTask.toFixed(2);
                        let mgrClass = '';
                        if (mgrData.totalTimeOffTask >= 2) mgrClass = 'time-off-task-high';
                        else if (mgrData.totalTimeOffTask >= 1) mgrClass = 'time-off-task-medium';

                        html += `<tr class="tot-manager-row" data-parent-day="${dayId}" data-manager-id="${managerId}" style="display: none; cursor: pointer;">`;
                        html += `<td></td><td style="padding-left: 20px;"><span class="expand-icon" id="icon-${managerId}">▶</span> <strong>${mgrData.manager}</strong></td>`;
                        html += `<td>${mgrData.employeeCount}</td><td class="${mgrClass}">${mgrHours}</td></tr>`;

                        if (mgrData.employees) {
                            mgrData.employees.forEach(emp => {
                                const timeDetailsUrl = buildTimeDetailsURLForDay(emp.employeeId, day.dateStr);
                                const empHours = emp.timeOffTask.toFixed(2);
                                let empClass = '';
                                if (emp.timeOffTask >= 0.5) empClass = 'time-off-task-high';
                                else if (emp.timeOffTask >= 0.25) empClass = 'time-off-task-medium';
                                html += `<tr class="tot-employee-row" data-parent-manager="${managerId}" style="display: none;">`;
                                html += `<td></td><td style="padding-left: 40px;"><a href="${timeDetailsUrl}" target="_blank" class="employee-link" title="View Time Details">${emp.employeeId} - ${emp.employeeName}</a></td>`;
                                html += `<td>${emp.pctTimeOnTask.toFixed(1)}% on task</td><td class="${empClass}">${empHours}</td></tr>`;
                            });
                        }
                    });
                }
            });
            tbody.innerHTML = html;
            tfoot.innerHTML = `<tr><td></td><td><strong>Total</strong></td><td></td><td><strong>${Math.round(data.totalTimeOffTask)}</strong></td></tr>`;

            tbody.querySelectorAll('.tot-day-row').forEach(row => {
                row.addEventListener('click', function() {
                    const dayId = this.dataset.dayId;
                    const icon = document.getElementById('icon-' + dayId);
                    const managerRows = tbody.querySelectorAll(`tr[data-parent-day="${dayId}"]`);
                    const isExpanded = icon.textContent === '▼';
                    managerRows.forEach(mgrRow => {
                        mgrRow.style.setProperty('display', isExpanded ? 'none' : 'table-row', 'important');
                        if (isExpanded) {
                            const managerId = mgrRow.dataset.managerId;
                            const mgrIcon = document.getElementById('icon-' + managerId);
                            if (mgrIcon) mgrIcon.textContent = '▶';
                            tbody.querySelectorAll(`tr[data-parent-manager="${managerId}"]`).forEach(empRow => {
                                empRow.style.setProperty('display', 'none', 'important');
                            });
                        }
                    });
                    icon.textContent = isExpanded ? '▶' : '▼';
                });
            });
            tbody.querySelectorAll('.tot-manager-row').forEach(row => {
                row.addEventListener('click', function(e) {
                    e.stopPropagation();
                    const managerId = this.dataset.managerId;
                    const icon = document.getElementById('icon-' + managerId);
                    const employeeRows = tbody.querySelectorAll(`tr[data-parent-manager="${managerId}"]`);
                    const isExpanded = icon.textContent === '▼';
                    employeeRows.forEach(empRow => {
                        empRow.style.setProperty('display', isExpanded ? 'none' : 'table-row', 'important');
                    });
                    icon.textContent = isExpanded ? '▶' : '▼';
                });
            });
            return;
        }

        thead.innerHTML = '<tr><th style="width: 40px;"></th><th>Manager</th><th>Employees</th><th>Time Off Task (hrs)</th></tr>';
        let html = '';
        let totalEmployees = 0;
        let totalTimeOffTask = 0;

        data.managerSummary.forEach((mgrData, mgrIndex) => {
            const managerId = `tot-mgr-${mgrIndex}`;
            totalEmployees += mgrData.employeeCount;
            totalTimeOffTask += mgrData.totalTimeOffTask;
            const totHours = mgrData.totalTimeOffTask.toFixed(2);
            let totClass = '';
            if (mgrData.totalTimeOffTask >= 2) totClass = 'time-off-task-high';
            else if (mgrData.totalTimeOffTask >= 1) totClass = 'time-off-task-medium';

            html += '<tr class="tot-manager-row" data-manager-id="' + managerId + '">';
            html += '<td><span class="expand-icon" id="icon-' + managerId + '">▼</span></td>';
            html += '<td><strong>' + mgrData.manager + '</strong></td>';
            html += '<td>' + mgrData.employeeCount + '</td>';
            html += '<td class="' + totClass + '">' + totHours + '</td></tr>';

            mgrData.employees.forEach(emp => {
                const timeDetailsUrl = buildTimeDetailsURL(emp.employeeId);
                const empTotHours = emp.timeOffTask.toFixed(2);
                let empTotClass = '';
                if (emp.timeOffTask >= 0.5) empTotClass = 'time-off-task-high';
                else if (emp.timeOffTask >= 0.25) empTotClass = 'time-off-task-medium';
                html += '<tr class="tot-employee-row" data-parent="' + managerId + '">';
                html += '<td></td>';
                html += '<td><a href="' + timeDetailsUrl + '" target="_blank" class="employee-link" title="View Time Details">' + emp.employeeId + ' - ' + emp.employeeName + '</a></td>';
                html += '<td>' + emp.pctTimeOnTask.toFixed(1) + '% on task</td>';
                html += '<td class="' + empTotClass + '">' + empTotHours + '</td></tr>';
            });
        });

        tbody.innerHTML = html;
        forceReflow(tbody);
        tfoot.innerHTML = '<tr><td></td><td><strong>Total</strong></td><td id="totalTOTEmployees">0</td><td id="totalTOTHours">0</td></tr>';
        const totalTOTEmployees = container.querySelector('#totalTOTEmployees');
        const totalTOTHours = container.querySelector('#totalTOTHours');
        if (totalTOTEmployees) totalTOTEmployees.textContent = totalEmployees;
        if (totalTOTHours) totalTOTHours.textContent = totalTimeOffTask.toFixed(2);

        tbody.querySelectorAll('.tot-manager-row').forEach(row => {
            row.addEventListener('click', function() {
                const managerId = this.dataset.managerId;
                const icon = document.getElementById('icon-' + managerId);
                const employeeRows = tbody.querySelectorAll('tr[data-parent="' + managerId + '"]');
                const isExpanded = icon.textContent === '▲';
                employeeRows.forEach(empRow => {
                    empRow.style.setProperty('display', isExpanded ? 'none' : 'table-row', 'important');
                });
                icon.textContent = isExpanded ? '▼' : '▲';
            });
        });
    }


    function renderDepartmentOverviewTable(data) {
        if (!data || data.length === 0) return;

        // Container already exists in HTML
        const container = document.getElementById('deptOverviewContainer');
        if (!container) return;

        const tbody = container.querySelector('#deptOverviewTableBody');
        if (!tbody) return;

        let html = '';
        const totals = {
            tpVol: 0, tpHrs: 0, tpVar: 0,
            ibVol: 0, ibHrs: 0, ibVar: 0,
            obVol: 0, obHrs: 0, obVar: 0,
            tsoVol: 0, tsoHrs: 0, tsoVar: 0,
            rlVol: 0, rlHrs: 0, rlVar: 0,
            icqaVol: 0, icqaHrs: 0, icqaVar: 0
        };

        function formatVol(val) {
            const num = normalizeNumber(val);
            return Math.round(num).toLocaleString('en-US');
        }

        function formatRate(val) {
            const num = normalizeNumber(val);
            return num.toFixed(1);
        }

        function formatHrs(val) {
            const num = normalizeNumber(val);
            return Math.round(num);
        }

        function formatVar(val) {
            const num = normalizeNumber(val);
            const rounded = Math.round(num);
            if (num < -2) {
                return '<span class="negative-diff">' + rounded + '</span>';
            } else if (num > 2) {
                return '<span class="positive-diff">' + rounded + '</span>';
            }
            return rounded;
        }

        function parseNum(val) {
            return normalizeNumber(val);
        }

        data.forEach(hourData => {
            // Accumulate totals
            totals.tpVol += parseNum(hourData.throughput?.vol);
            totals.tpHrs += parseNum(hourData.throughput?.hrs);
            totals.tpVar += parseNum(hourData.throughput?.varHrs);

            totals.ibVol += parseNum(hourData.ibTot?.vol);
            totals.ibHrs += parseNum(hourData.ibTot?.hrs);
            totals.ibVar += parseNum(hourData.ibTot?.varHrs);

            totals.obVol += parseNum(hourData.obTotSummary?.vol);
            totals.obHrs += parseNum(hourData.obTotSummary?.hrs);
            totals.obVar += parseNum(hourData.obTotSummary?.varHrs);

            totals.tsoVol += parseNum(hourData.tsoTotSummary?.vol);
            totals.tsoHrs += parseNum(hourData.tsoTotSummary?.hrs);
            totals.tsoVar += parseNum(hourData.tsoTotSummary?.varHrs);

            totals.rlVol += parseNum(hourData.revLogSummary?.vol);
            totals.rlHrs += parseNum(hourData.revLogSummary?.hrs);
            totals.rlVar += parseNum(hourData.revLogSummary?.varHrs);

            totals.icqaVol += parseNum(hourData.icqaTot?.vol);
            totals.icqaHrs += parseNum(hourData.icqaTot?.hrs);
            totals.icqaVar += parseNum(hourData.icqaTot?.varHrs);

            html += '<tr>';
            html += '<td>' + buildPPRTimeLink(hourData.timeRange, hourData.dateStr) + '</td>';
            // Throughput
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.throughput?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.throughput?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.throughput?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.throughput?.varHrs) + '</td>';
            // Inbound
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.ibTot?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.ibTot?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.ibTot?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.ibTot?.varHrs) + '</td>';
            // Outbound
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.obTotSummary?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.obTotSummary?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.obTotSummary?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.obTotSummary?.varHrs) + '</td>';
            // Transfer Out
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.tsoTotSummary?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.tsoTotSummary?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.tsoTotSummary?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.tsoTotSummary?.varHrs) + '</td>';
            // Reverse Logistics
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.revLogSummary?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.revLogSummary?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.revLogSummary?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.revLogSummary?.varHrs) + '</td>';
            // ICQA
            html += '<td style="border-left: 2px solid #146eb4;">' + formatVol(hourData.icqaTot?.vol) + '</td>';
            html += '<td>' + formatRate(hourData.icqaTot?.rate) + '</td>';
            html += '<td>' + formatHrs(hourData.icqaTot?.hrs) + '</td>';
            html += '<td>' + formatVar(hourData.icqaTot?.varHrs) + '</td>';
            html += '</tr>';
        });

        tbody.innerHTML = html;

        // Update footer totals
        const setTotal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };
        const setTotalVar = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.innerHTML = formatVar(val);
        };

        // Calculate weighted average TPH (Vol / Hrs)
        const calcTPH = (vol, hrs) => {
            if (hrs === 0) return '-';
            return (vol / hrs).toFixed(1);
        };

        setTotal('totalTPVol', Math.round(totals.tpVol).toLocaleString('en-US'));
        setTotal('totalTPTPH', calcTPH(totals.tpVol, totals.tpHrs));
        setTotal('totalTPHrs', Math.round(totals.tpHrs));
        setTotalVar('totalTPVar', totals.tpVar);

        setTotal('totalIBVol', Math.round(totals.ibVol).toLocaleString('en-US'));
        setTotal('totalIBTPH', calcTPH(totals.ibVol, totals.ibHrs));
        setTotal('totalIBHrs', Math.round(totals.ibHrs));
        setTotalVar('totalIBVar', totals.ibVar);

        setTotal('totalOBVol2', Math.round(totals.obVol).toLocaleString('en-US'));
        setTotal('totalOBTPH', calcTPH(totals.obVol, totals.obHrs));
        setTotal('totalOBHrs', Math.round(totals.obHrs));
        setTotalVar('totalOBVar', totals.obVar);

        setTotal('totalTSOVol', Math.round(totals.tsoVol).toLocaleString('en-US'));
        setTotal('totalTSOTPH', calcTPH(totals.tsoVol, totals.tsoHrs));
        setTotal('totalTSOHrs', Math.round(totals.tsoHrs));
        setTotalVar('totalTSOVar', totals.tsoVar);

        setTotal('totalRLVol', Math.round(totals.rlVol).toLocaleString('en-US'));
        setTotal('totalRLTPH', calcTPH(totals.rlVol, totals.rlHrs));
        setTotal('totalRLHrs', Math.round(totals.rlHrs));
        setTotalVar('totalRLVar', totals.rlVar);

        setTotal('totalICQAVol', Math.round(totals.icqaVol).toLocaleString('en-US'));
        setTotal('totalICQATPH', calcTPH(totals.icqaVol, totals.icqaHrs));
        setTotal('totalICQAHrs', Math.round(totals.icqaHrs));
        setTotalVar('totalICQAVar', totals.icqaVar);
    }

    function parseEmployeeData(html, functionName) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const functionTables = doc.querySelectorAll('table[id^="function-"]');

        let targetTable = null;
        for (const table of functionTables) {
            const caption = table.querySelector('caption');
            if (caption && caption.textContent.includes(functionName)) {
                targetTable = table;
                break;
            }
        }

        // If not found, try case-insensitive search or partial match
        if (!targetTable) {
            const lowerFunctionName = functionName.toLowerCase();
            for (const table of functionTables) {
                const caption = table.querySelector('caption');
                if (caption && caption.textContent.toLowerCase().includes(lowerFunctionName)) {
                    targetTable = table;
                    break;
                }
            }
        }

        if (!targetTable) {
            return [];
        }

        const employees = [];
        const tbody = targetTable.querySelector('tbody');
        if (!tbody) {
            return [];
        }

        // Find the header row to determine the "Paid Hours" or "Total" column index
        const thead = targetTable.querySelector('thead');
        let paidHoursColIndex = -1;
        let totalColIndex = -1;
        
        if (thead) {
            const headerRows = thead.querySelectorAll('tr');
            for (const headerRow of headerRows) {
                const headerCells = headerRow.querySelectorAll('th, td');
                let colPosition = 0;
                for (let i = 0; i < headerCells.length; i++) {
                    const headerText = headerCells[i].textContent.trim().toLowerCase();
                    const colspan = parseInt(headerCells[i].getAttribute('colspan')) || 1;
                    
                    // Look for "Paid Hours" column
                    if (headerText === 'paid hours' || headerText === 'total paid hours') {
                        paidHoursColIndex = colPosition;
                    }
                    // Also track "Total" column as fallback
                    if (headerText === 'total' && colPosition > 3) {
                        totalColIndex = colPosition;
                    }
                    
                    colPosition += colspan;
                }
                if (paidHoursColIndex !== -1) break;
            }
        }
        
        // Use Total column if Paid Hours not found
        if (paidHoursColIndex === -1 && totalColIndex !== -1) {
            paidHoursColIndex = totalColIndex;
        }

        const rows = tbody.querySelectorAll('tr.empl-all');
        
        for (const row of rows) {
            if (row.classList.contains('total')) continue;

            const cells = row.querySelectorAll('td');
            if (cells.length < 5) continue;

            const idLink = cells[1]?.querySelector('a');
            const nameLink = cells[2]?.querySelector('a');
            const managerCell = cells[3];
            
            let totalHours = '0';
            
            // Method 1 (primary): Look for the first cell with class "size-total" + "highlighted"
            // This is the Paid Hours Total column in FCLM tables with size breakdowns
            const sizeTotalCell = row.querySelector('td.size-total.highlighted');
            if (sizeTotalCell) {
                const val = sizeTotalCell.textContent.trim();
                if (val && normalizeNumber(val) !== 0) {
                    totalHours = val;
                }
            }
            
            // Method 2: Use the column index from header if found
            // Note: paidHoursColIndex points to the START of the "Paid Hours" group (Small column),
            // so we need to find the Total sub-column. For tables without size breakdowns,
            // paidHoursColIndex points directly to the single Paid Hours column.
            if (totalHours === '0' && paidHoursColIndex !== -1 && cells[paidHoursColIndex]) {
                const val = cells[paidHoursColIndex].textContent.trim();
                if (val && normalizeNumber(val) > 0) {
                    totalHours = val;
                }
            }
            
            // Method 3: Look for any cell with class "size-total"
            if (totalHours === '0') {
                const sizeTotalCells = row.querySelectorAll('td.size-total');
                for (const cell of sizeTotalCells) {
                    const val = cell.textContent.trim();
                    if (val && normalizeNumber(val) !== 0) {
                        totalHours = val;
                    }
                }
            }
            
            // Method 4: Fallback - scan cells 4-12 for a value that looks like hours
            // Look for the LAST value in the 0.01-24 range before we hit large numbers (counts)
            if (totalHours === '0') {
                let lastHoursCandidate = '0';
                for (let i = 4; i < Math.min(cells.length, 12); i++) {
                    const cellText = cells[i]?.textContent.trim();
                    if (cellText) {
                        const val = normalizeNumber(cellText);
                        // Hours should be reasonable (0.01 to 24 range for daily)
                        if (val > 0 && val <= 24) {
                            lastHoursCandidate = cellText;
                        }
                    }
                }
                totalHours = lastHoursCandidate;
            }

            if (idLink && nameLink) {
                employees.push({
                    id: idLink.textContent.trim(),
                    name: nameLink.textContent.trim(),
                    manager: managerCell?.textContent.trim() || 'No Manager',
                    hours: totalHours
                });
            }
        }

        return employees;
    }

    function parseProcessPathData(html, lineItemId) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        const row = doc.getElementById(lineItemId);

        if (!row) return { vol: '0', hrs: '0', rate: '0', planRate: '0', varHrs: '0' };

        const volCell = row.querySelector('td.actualVolume');
        const vol = volCell?.querySelector('div.original')?.textContent.trim() || '0';

        const hrsCell = row.querySelector('td.actualTimeSeconds');
        const hrs = hrsCell?.querySelector('div.original')?.textContent.trim() || '0';

        const rateCell = row.querySelector('td.actualProductivity.rate');
        const rate = rateCell?.querySelector('div.original')?.textContent.trim() || '0';

        // Get Plan Rate from td.planProductivity
        const planRateCell = row.querySelector('td.planProductivity');
        const planRate = planRateCell?.querySelector('div.original')?.textContent.trim() || '0';

        // Get Plan Variance Hours from td.planVarianceSeconds
        const varHrsCell = row.querySelector('td.planVarianceSeconds');
        const varHrs = varHrsCell?.querySelector('div.original')?.textContent.trim() || '0';

        return { vol, hrs, rate, planRate, varHrs };
    }

    function fetchURL(url, retries = MAX_RETRIES) {
        return new Promise((resolve, reject) => {
            function attemptFetch(attemptsLeft) {
                GM_xmlhttpRequest({
                    method: 'GET',
                    url: url,
                    timeout: 15000,
                    headers: {
                        'Accept-Language': 'en-US,en;q=0.9'
                    },
                    onload: function(response) {
                        if (response.status === 200) {
                            resolve(response.responseText);
                        } else {
                            if (attemptsLeft > 0) {
                                console.warn(`⚠️ HTTP ${response.status}, retrying... (${attemptsLeft} left)`);
                                setTimeout(() => attemptFetch(attemptsLeft - 1), 2000);
                            } else {
                                reject(new Error(`HTTP ${response.status}`));
                            }
                        }
                    },
                    onerror: function(error) {
                        if (attemptsLeft > 0) {
                            console.warn(`⚠️ Network error, retrying... (${attemptsLeft} left)`);
                            setTimeout(() => attemptFetch(attemptsLeft - 1), 2000);
                        } else {
                            reject(error);
                        }
                    },
                    ontimeout: function() {
                        if (attemptsLeft > 0) {
                            console.warn(`⚠️ Timeout, retrying... (${attemptsLeft} left)`);
                            setTimeout(() => attemptFetch(attemptsLeft - 1), 2000);
                        } else {
                            reject(new Error('Timeout'));
                        }
                    }
                });
            }

            attemptFetch(retries);
        });
    }

    function parseHoursFromHTML(html, functionName) {
        const parser = new DOMParser();
        const doc = parser.parseFromString(html, 'text/html');
        
        // Primary method: find the function's own table by caption
        const tables = doc.querySelectorAll('table');
        for (const table of tables) {
            const caption = table.querySelector('caption');
            if (!caption) continue;
            const captionText = caption.textContent.trim();
            const funcName = captionText.split('[')[0].trim();
            if (funcName !== functionName) continue;
            
            // Found the function table — get total hours from the total row
            const totalRow = table.querySelector('tr.total, tr.empl-all.total');
            if (totalRow) {
                // Total Paid Hours is the first td.size-total that is NOT .numeric
                const cells = totalRow.querySelectorAll('td.size-total');
                for (const cell of cells) {
                    if (!cell.classList.contains('numeric')) {
                        const val = cell.textContent.trim();
                        const parsed = normalizeNumber(val);
                        if (val && parsed !== 0) {
                            return String(parsed);
                        }
                    }
                }
                // Fallback: td.numeric.size-total.highlighted
                const numCell = totalRow.querySelector('td.numeric.size-total.highlighted');
                if (numCell) return numCell.textContent.trim();
            }
        }
        
        // Fallback: summary table approach (scan forward from function name to next Total row)
        const summaryTable = doc.querySelector('table#summary.result-table tbody');
        if (summaryTable) {
            const rows = summaryTable.querySelectorAll('tr.empl-all');
            let found = false;
            for (let i = 0; i < rows.length; i++) {
                const row = rows[i];
                const linkElement = row.querySelector('th a');
                if (linkElement && linkElement.textContent.trim() === functionName) {
                    found = true;
                    continue;
                }
                if (found) {
                    const sizeCell = row.querySelector('td.size-total.highlighted');
                    if (sizeCell && sizeCell.textContent.trim() === 'Total') {
                        const hoursCell = row.querySelector('td.numeric.size-total.highlighted');
                        if (hoursCell) return hoursCell.textContent.trim();
                    }
                }
            }
        }
        
        return '0';
    }

    function updateLoadingBar() {
        const loadingBar = document.getElementById('loadingBar');
        const loadingProgress = document.getElementById('loadingProgress');
        const loadingText = document.getElementById('loadingText');

        if (loadingBar && loadingProgress && loadingText) {
            const percentage = Math.min(100, Math.round((currentFetchStep / totalFetchSteps) * 100));
            loadingProgress.style.width = percentage + '%';
            loadingText.textContent = `${currentFetchStep}/${totalFetchSteps} (${percentage}%)`;
            loadingBar.style.display = 'flex';
        }
    }

    function hideLoadingBar() {
        const loadingBar = document.getElementById('loadingBar');
        if (loadingBar) {
            setTimeout(() => {
                loadingBar.style.display = 'none';
            }, 500);
        }
    }

    function updateLastUpdateTime() {
        const now = new Date();
        lastUpdateTime = now;

        const lastUpdateEl = document.getElementById('lastUpdateTime');
        if (lastUpdateEl) {
            const timeStr = now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            lastUpdateEl.textContent = timeStr;
        }
    }

    function updateSwitchText() {
        const autoRefreshToggle = document.getElementById('autoRefreshToggle');
        const switchText = document.getElementById('switchText');

        if (!autoRefreshToggle || !switchText) return;

        if (autoRefreshToggle.checked) {
            switchText.textContent = 'ON';
            switchText.style.color = '#3fb618';
        } else {
            switchText.textContent = 'OFF';
            switchText.style.color = '#888';
        }
    }

    function startCountdown() {
        if (countdownInterval) {
            clearInterval(countdownInterval);
        }

        const autoRefreshToggle = document.getElementById('autoRefreshToggle');
        const refreshIntervalSelect = document.getElementById('refreshInterval');

        if (!autoRefreshToggle || !refreshIntervalSelect) return;

        if (!autoRefreshToggle.checked) {
            const nextRefreshEl = document.getElementById('nextRefreshTime');
            if (nextRefreshEl) {
                nextRefreshEl.textContent = '--:--';
            }
            return;
        }

        const intervalSeconds = parseInt(refreshIntervalSelect.value);

        countdownInterval = setInterval(() => {
            if (!lastUpdateTime) return;

            const now = new Date();
            const elapsed = Math.floor((now - lastUpdateTime) / 1000);
            const remaining = intervalSeconds - elapsed;

            const nextRefreshEl = document.getElementById('nextRefreshTime');
            if (nextRefreshEl) {
                if (remaining > 0) {
                    const mins = Math.floor(remaining / 60);
                    const secs = remaining % 60;
                    nextRefreshEl.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
                } else {
                    nextRefreshEl.textContent = '00:00';
                }
            }
        }, 1000);
    }

    function startAutoRefresh() {
        const autoRefreshToggle = document.getElementById('autoRefreshToggle');
        const refreshIntervalSelect = document.getElementById('refreshInterval');

        if (!autoRefreshToggle || !refreshIntervalSelect) return;

        if (autoRefreshTimer) {
            clearTimeout(autoRefreshTimer);
            autoRefreshTimer = null;
        }

        if (!autoRefreshToggle.checked) {
            return;
        }

        const intervalSeconds = parseInt(refreshIntervalSelect.value);
        const intervalMs = intervalSeconds * 1000;

        autoRefreshTimer = setTimeout(() => {
            fetchAllData();
        }, intervalMs);
    }

    function showErrorNotification() {
        if (failedSlots.length === 0) return;

        let errorBanner = document.getElementById('errorBanner');
        if (!errorBanner) {
            errorBanner = document.createElement('div');
            errorBanner.id = 'errorBanner';
            errorBanner.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                background: #d13212;
                color: white;
                padding: 15px 20px;
                border-radius: 8px;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
                z-index: 9999;
                max-width: 400px;
                font-family: 'Amazon Ember', Arial, sans-serif;
                font-size: 14px;
            `;
            document.body.appendChild(errorBanner);
        }

        const slotsList = failedSlots.map(slot => `<li>${slot}</li>`).join('');
        errorBanner.innerHTML = `
            <div style="font-weight: bold; margin-bottom: 8px;">⚠️ Failed to load data:</div>
            <ul style="margin: 5px 0 10px 20px; padding: 0;">
                ${slotsList}
            </ul>
            <button onclick="this.parentElement.remove(); document.getElementById('refreshBtn').click();"
                    style="background: white; color: #d13212; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: bold;">
                🔄 Retry Now
            </button>
            <button onclick="this.parentElement.remove();"
                    style="background: transparent; color: white; border: 1px solid white; padding: 8px 16px; border-radius: 4px; cursor: pointer; margin-left: 8px;">
                Dismiss
            </button>
        `;

        setTimeout(() => {
            if (errorBanner && errorBanner.parentElement) {
                errorBanner.style.opacity = '0';
                errorBanner.style.transition = 'opacity 0.5s';
                setTimeout(() => errorBanner.remove(), 500);
            }
        }, 30000);
    }

    async function fetchSummaryData() {
        const url1 = buildSummaryURL('01002960');
        const url2 = buildSummaryURL('01003028');
        const url3 = buildSummaryURL('01003047');

        if (!url1 || !url2 || !url3) {
            console.error('❌ Cannot build URLs without FC code');
            alert('⚠️ Please enter FC code first!');
            return null;
        }

        try {
            const [html1, html2, html3] = await Promise.all([
                fetchURL(url1),
                fetchURL(url2),
                fetchURL(url3)
            ]);

            currentFetchStep += 3;
            updateLoadingBar();

            // Get bucket configuration
            const bucketConfig = getBucketConfig();
            
            // Map HTML responses by process key
            const htmlByProcess = {
                admin: html1,
                facilities: html2,
                nonfc: html3
            };
            
            // Calculate hours for each bucket using config
            const bucketHours = calculateBucketHours(htmlByProcess, bucketConfig);
            
            // Calculate employee data for each bucket using config
            const bucketEmployees = calculateBucketEmployees(htmlByProcess, bucketConfig);
            
            // Update global employeeData
            employeeData = bucketEmployees;

            return bucketHours;
        } catch (error) {
            console.error('❌ Summary fetch error:', error);
            failedSlots.push('Summary');
            currentFetchStep += 3;
            updateLoadingBar();
            
            // Return empty data for all configured buckets
            const bucketConfig = getBucketConfig();
            const emptyResult = {};
            bucketConfig.buckets.forEach(bucket => {
                emptyResult[bucket.id] = '0';
            });
            return emptyResult;
        }
    }

    // Helper function to generate time slots for hourly data fetching
    // Handles cross-midnight scenarios (e.g., 23:00 D1 to 06:00 D2)
    // Filter out time slots that start in the future (no data to fetch yet)
    function filterFutureSlots(timeSlots) {
        const now = new Date();
        const currentDateStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
        const currentHourNow = now.getHours();

        return timeSlots.filter(slot => {
            if (slot.startDateStr < currentDateStr) return true;   // past date — keep
            if (slot.startDateStr > currentDateStr) return false;  // future date — drop
            // Same date — keep only if the slot's start hour has already begun
            return slot.startHour <= currentHourNow;
        });
    }

    function generateTimeSlots(params, startDateStr) {
        const timeSlots = [];
        
        let startHour = 0;
        let startMinute = 0;
        let maxHour = 23;
        let endMinute = 0;
        let endDateStr = startDateStr;
        
        if (params.spanType === 'Intraday') {
            startHour = params.startHourNum;
            startMinute = params.startMinuteNum || 0;
            maxHour = params.endHourNum;
            endMinute = params.endMinuteNum || 0;
            endDateStr = params.endDateStr || startDateStr;
            
            // Check if this is a cross-midnight scenario (start date != end date OR startHour > endHour on same date)
            const isCrossMidnight = (startDateStr !== endDateStr) || (startHour > maxHour);
            
            if (isCrossMidnight) {
                // Part 1: From startHour to 23 on startDate
                for (let hour = startHour; hour <= 23; hour++) {
                    let slotStartMinute = (hour === startHour) ? startMinute : 0;
                    let nextHour = hour + 1;
                    let slotEndDateStr = startDateStr;
                    
                    if (nextHour === 24) {
                        nextHour = 0;
                        slotEndDateStr = endDateStr;
                    }
                    
                    timeSlots.push({
                        startDateStr: startDateStr,
                        startHour: hour,
                        startMinute: slotStartMinute,
                        endDateStr: slotEndDateStr,
                        endHour: nextHour,
                        endMinute: 0
                    });
                }
                
                // Part 2: From 0 to maxHour on endDate
                const lastFullHour = endMinute === 0 ? maxHour - 1 : maxHour - 1;
                
                for (let hour = 0; hour <= lastFullHour; hour++) {
                    let nextHour = hour + 1;
                    
                    timeSlots.push({
                        startDateStr: endDateStr,
                        startHour: hour,
                        startMinute: 0,
                        endDateStr: endDateStr,
                        endHour: nextHour,
                        endMinute: 0
                    });
                }
                
                // Add partial last hour if endMinute > 0
                if (endMinute > 0) {
                    timeSlots.push({
                        startDateStr: endDateStr,
                        startHour: maxHour,
                        startMinute: 0,
                        endDateStr: endDateStr,
                        endHour: maxHour,
                        endMinute: endMinute
                    });
                }
                
                // Filter out future slots for cross-midnight Intraday
                return filterFutureSlots(timeSlots);
            }
        } else {
            // Day mode - check if today or past date
            const selectedDate = new Date(startDateStr);
            const today = new Date();
            today.setHours(0, 0, 0, 0);
            selectedDate.setHours(0, 0, 0, 0);
            
            if (selectedDate.getTime() === today.getTime()) {
                maxHour = new Date().getHours();
            } else if (selectedDate.getTime() > today.getTime()) {
                return [];
            }
        }
        
        // Standard same-day slot generation
        for (let hour = startHour; hour <= maxHour; hour++) {
            let slotStartMinute = 0;
            let slotEndMinute = 0;
            let nextHour = hour + 1;
            let slotEndDateStr = startDateStr;

            if (params.spanType === 'Intraday') {
                if (hour === startHour) {
                    slotStartMinute = startMinute;
                }
                if (hour === maxHour) {
                    nextHour = maxHour;
                    slotEndMinute = endMinute;
                    if (endMinute === 0 && hour === maxHour) {
                        continue;
                    }
                }
            }

            if (nextHour === 24) {
                nextHour = 0;
                const nextDay = new Date(startDateStr);
                nextDay.setDate(nextDay.getDate() + 1);
                slotEndDateStr = nextDay.toISOString().split('T')[0];
            }

            timeSlots.push({
                startDateStr: startDateStr,
                startHour: hour,
                startMinute: slotStartMinute,
                endDateStr: slotEndDateStr,
                endHour: nextHour,
                endMinute: slotEndMinute
            });
        }
        
        // Filter out future slots for Intraday mode (Day mode already handles this above)
        if (params.spanType === 'Intraday') {
            return filterFutureSlots(timeSlots);
        }
        
        return timeSlots;
    }

    // Helper function to generate day slots for Week mode
    // Returns array of {dateStr, dayName, dayIndex} for each day in the week
    // Amazon weeks start on Sunday
    function generateDaySlots(params) {
        const daySlots = [];
        const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
        
        if (params.spanType !== 'Week' || !params.startDateStr) {
            return daySlots;
        }
        
        const daysToFetch = params.daysToFetch || 7;
        // Parse date parts to avoid UTC timezone issues with new Date('YYYY-MM-DD')
        const dateParts = params.startDateStr.split('-');
        const startYear = parseInt(dateParts[0]);
        const startMonth = parseInt(dateParts[1]) - 1;
        const startDay = parseInt(dateParts[2]);
        
        for (let i = 0; i < daysToFetch; i++) {
            const currentDate = new Date(startYear, startMonth, startDay + i);
            const dateStr = currentDate.getFullYear() + '-' + String(currentDate.getMonth() + 1).padStart(2, '0') + '-' + String(currentDate.getDate()).padStart(2, '0');
            
            daySlots.push({
                dateStr: dateStr,
                dayName: dayNames[i],
                dayIndex: i
            });
        }
        
        return daySlots;
    }

    async function fetchHourlyData() {
        const params = getFormParameters();
        if (!params) return [];

        const hourlyDataResult = [];

        const startDateStr = params.startDateStr;
        if (!startDateStr) return [];

        // Handle Week mode - fetch daily data instead of hourly
        if (params.spanType === 'Week') {
            const daySlots = generateDaySlots(params);
            if (daySlots.length === 0) {
                return [];
            }

            // Fetch all days in parallel
            const dayPromises = daySlots.map(async (dayData) => {
                const url1 = buildDailyURL('01002960', dayData.dateStr);
                const url2 = buildDailyURL('01003028', dayData.dateStr);
                const url3 = buildDailyURL('01003047', dayData.dateStr);

                if (!url1 || !url2 || !url3) return null;

                try {
                    const [html1, html2, html3] = await Promise.all([
                        fetchURL(url1),
                        fetchURL(url2),
                        fetchURL(url3)
                    ]);

                    currentFetchStep += 3;
                    updateLoadingBar();

                    // Get bucket configuration and calculate hours dynamically
                    const bucketConfig = getBucketConfig();
                    const htmlByProcess = { admin: html1, facilities: html2, nonfc: html3 };
                    const bucketHours = calculateBucketHours(htmlByProcess, bucketConfig);
                    
                    return {
                        timeRange: dayData.dayName,
                        dateStr: dayData.dateStr,
                        dayIndex: dayData.dayIndex,
                        ...bucketHours
                    };
                } catch (error) {
                    console.error(`❌ Error fetching indirect for ${dayData.dayName}:`, error);
                    failedSlots.push(`Indirect ${dayData.dayName}`);
                    currentFetchStep += 3;
                    updateLoadingBar();
                    return null;
                }
            });

            const results = await Promise.all(dayPromises);
            results.forEach(result => {
                if (result) hourlyDataResult.push(result);
            });

            // Sort by day index to ensure correct order
            hourlyDataResult.sort((a, b) => a.dayIndex - b.dayIndex);
            
            return hourlyDataResult;
        }

        // Original hourly logic for Day/Intraday modes
        const timeSlots = generateTimeSlots(params, startDateStr);
        
        if (timeSlots.length === 0) {
            return [];
        }

        // Batch fetch in groups of 8 for better parallelization
        const BATCH_SIZE = 8;
        for (let i = 0; i < timeSlots.length; i += BATCH_SIZE) {
            const batch = timeSlots.slice(i, i + BATCH_SIZE);
            
            const batchPromises = batch.map(async (slotData) => {
                const url1 = buildHourlyURLWithMinutes('01002960', slotData.startDateStr, slotData.startHour, slotData.startMinute, slotData.endDateStr, slotData.endHour, slotData.endMinute);
                const url2 = buildHourlyURLWithMinutes('01003028', slotData.startDateStr, slotData.startHour, slotData.startMinute, slotData.endDateStr, slotData.endHour, slotData.endMinute);
                const url3 = buildHourlyURLWithMinutes('01003047', slotData.startDateStr, slotData.startHour, slotData.startMinute, slotData.endDateStr, slotData.endHour, slotData.endMinute);

                if (!url1 || !url2 || !url3) return null;

                // Build time range label
                const startMinStr = String(slotData.startMinute).padStart(2, '0');
                const endHourDisplay = slotData.endHour === 0 && slotData.endMinute === 0 ? '24' : String(slotData.endHour).padStart(2, '0');
                const endMinStr = String(slotData.endMinute).padStart(2, '0');
                const timeRange = `${String(slotData.startHour).padStart(2, '0')}:${startMinStr}-${endHourDisplay}:${endMinStr}`;

                try {
                    const [html1, html2, html3] = await Promise.all([
                        fetchURL(url1),
                        fetchURL(url2),
                        fetchURL(url3)
                    ]);

                    currentFetchStep += 3;
                    updateLoadingBar();

                    // Get bucket configuration and calculate hours dynamically
                    const bucketConfig = getBucketConfig();
                    const htmlByProcess = { admin: html1, facilities: html2, nonfc: html3 };
                    const bucketHours = calculateBucketHours(htmlByProcess, bucketConfig);
                    
                    return {
                        timeRange,
                        dateStr: slotData.startDateStr,
                        ...bucketHours
                    };
                } catch (error) {
                    console.error(`❌ Error fetching indirect slot ${timeRange}:`, error);
                    failedSlots.push(`Indirect ${timeRange}`);
                    currentFetchStep += 3;
                    updateLoadingBar();
                    return null;
                }
            });

            const batchResults = await Promise.all(batchPromises);
            batchResults.forEach(result => {
                if (result) hourlyDataResult.push(result);
            });
        }

        return hourlyDataResult;
    }

    async function fetchHourlyProcessPathData() {
        const params = getFormParameters();
        if (!params) return [];

        const processPathDataResult = [];

        const startDateStr = params.startDateStr;
        if (!startDateStr) return [];

        // Handle Week mode - fetch daily data instead of hourly
        if (params.spanType === 'Week') {
            const daySlots = generateDaySlots(params);
            if (daySlots.length === 0) {
                return [];
            }

            // Fetch all days in parallel (use Intraday 00:00-24:00 to match Day view)
            const dayPromises = daySlots.map(async (dayData) => {
                // Calculate next day string without Date object timezone issues
                const parts = dayData.dateStr.split('-');
                const y = parseInt(parts[0]), m = parseInt(parts[1]) - 1, d = parseInt(parts[2]);
                const nextDayDate = new Date(y, m, d + 1);
                const endDateStr = nextDayDate.getFullYear() + '-' + String(nextDayDate.getMonth() + 1).padStart(2, '0') + '-' + String(nextDayDate.getDate()).padStart(2, '0');
                const url = buildHourlyProcessPathURLWithMinutes(dayData.dateStr, 0, 0, endDateStr, 0, 0);
                if (!url) return null;

                try {
                    const html = await fetchURL(url);
                    currentFetchStep++;
                    updateLoadingBar();

                    // OB Indirects
                    const obTotal = parseProcessPathData(html, 'ppr.detail.outbound.outbound.total');
                    const obLeadPA = parseProcessPathData(html, 'ppr.detail.outbound.obLeadPA.obLeadPA');
                    const obProblemSolve = parseProcessPathData(html, 'ppr.detail.outbound.obDefect.obProblemSolve');
                    const packSupport = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSupport');
                    const pickSupport = parseProcessPathData(html, 'ppr.detail.outbound.pick.pickSupport');
                    const shipDock = parseProcessPathData(html, 'ppr.detail.outbound.ship.shipDock');

                    // OB Direct
                    const pick = parseProcessPathData(html, 'ppr.detail.outbound.pick.pick.total');
                    const packSingle = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSingle.total');
                    const spp = parseProcessPathData(html, 'ppr.detail.outbound.pack.packLevAutofold');
                    const sort = parseProcessPathData(html, 'ppr.detail.outbound.sort.sort.total');
                    const chuting = parseProcessPathData(html, 'ppr.detail.outbound.pack.chuting.total');
                    const packMultis = parseProcessPathData(html, 'ppr.detail.outbound.pack.packMultis.total');

                    // Transfer Out (DA)
                    const daBldgTransfer = parseProcessPathData(html, 'ppr.detail.da.da.total');
                    const transferOutPick = parseProcessPathData(html, 'ppr.detail.da.rcPick.transferOutPick.total');
                    const transferOut = parseProcessPathData(html, 'ppr.detail.da.transferOut.transferOut');
                    const transferOutDock = parseProcessPathData(html, 'ppr.detail.da.transferOutDock.transferOutDock');
                    const toLeadPA = parseProcessPathData(html, 'ppr.detail.da.toLeadPa.toLeadPa');

                    // Vendor Returns
                    const vretTot = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vendorReturns.total');
                    const vretPick = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPick.total');
                    const vretPack = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPack.total');
                    const vretSupport = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnSupport');

                    // Department Overview
                    const throughputRaw = parseProcessPathData(html, 'ppr.fcSummary.throughput');
                    const supportSummary = parseProcessPathData(html, 'ppr.fcSummary.support');
                    const ibTot = parseProcessPathData(html, 'ppr.fcSummary.totalInbound');
                    const obTotSummary = parseProcessPathData(html, 'ppr.fcSummary.outbound');
                    const tsoTotSummary = parseProcessPathData(html, 'ppr.fcSummary.transferOut');
                    const revLogSummary = parseProcessPathData(html, 'ppr.fcSummary.reverseLogistics');
                    const icqaTot = parseProcessPathData(html, 'ppr.detail.support.support.ICQACS');

                    // Deduct Support varHrs from Throughput varHrs
                    const tpVarHrsRaw = normalizeNumber(throughputRaw.varHrs);
                    const supportVarHrs = normalizeNumber(supportSummary.varHrs);
                    const adjustedTpVarHrs = tpVarHrsRaw - supportVarHrs;
                    const throughput = {
                        vol: throughputRaw.vol,
                        hrs: throughputRaw.hrs,
                        rate: throughputRaw.rate,
                        planRate: throughputRaw.planRate,
                        varHrs: adjustedTpVarHrs.toString()
                    };

                    // IB Indirects
                    const ibTotal = parseProcessPathData(html, 'ppr.fcSummary.totalInbound');
                    const receiveDock = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveDock');
                    const receiveSupport = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveSupport');
                    const tsiSupport = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInSupport');
                    const stpSupport = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.stowToPrimeSupport');
                    const rsrSupport = parseProcessPathData(html, 'ppr.detail.inbound.rsr.rsrSupport');
                    const ibLeadPA = parseProcessPathData(html, 'ppr.detail.inbound.ibLeadPa.ibLeadPa');

                    // IB Direct
                    const tsiVol = parseProcessPathData(html, 'ppr.fcSummary.transferIn');
                    const nvfVol = parseProcessPathData(html, 'ppr.fcSummary.vendorRec');
                    const eachTSI = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.eachTransferIn.total');
                    const tsiDecant = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInDecant');
                    const eachStP = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.eachStowToPrime.total');
                    const prepRecorder = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.prepRecorder.total');
                    const cubiscan = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.cubiscan');
                    
                    return {
                        timeRange: dayData.dayName,
                        dateStr: dayData.dateStr,
                        dayIndex: dayData.dayIndex,
                        // Department Overview (FC Summary)
                        throughput,
                        ibTot,
                        obTotSummary,
                        tsoTotSummary,
                        revLogSummary,
                        icqaTot,
                        // OB Indirects
                        obVol: obTotal.vol,
                        obTot: obTotal,
                        obLeadPA,
                        obProblemSolve,
                        packSupport,
                        pickSupport,
                        shipDock,
                        // OB Direct
                        pick,
                        packSingle,
                        spp,
                        sort,
                        chuting,
                        packMultis,
                        // Transfer Out
                        daBldgTransferVol: daBldgTransfer.vol,
                        daBldgTransfer,
                        transferOutPick,
                        transferOut,
                        transferOutDock,
                        toLeadPA,
                        // Vendor Returns
                        vretVol: vretSupport.vol,
                        vretTot,
                        vretPick,
                        vretPack,
                        vretSupport,
                        // IB Indirects
                        ibTotal,
                        receiveDock,
                        receiveSupport,
                        tsiSupport,
                        stpSupport,
                        rsrSupport,
                        ibLeadPA,
                        // IB Direct
                        tsiVol,
                        nvfVol,
                        eachTSI,
                        tsiDecant,
                        eachStP,
                        prepRecorder,
                        cubiscan
                    };
                } catch (error) {
                    console.error(`❌ Error fetching process path for ${dayData.dayName}:`, error);
                    failedSlots.push(`Process Path ${dayData.dayName}`);
                    currentFetchStep++;
                    updateLoadingBar();
                    return null;
                }
            });

            const results = await Promise.all(dayPromises);
            results.forEach(result => {
                if (result) processPathDataResult.push(result);
            });

            // Sort by day index to ensure correct order
            processPathDataResult.sort((a, b) => a.dayIndex - b.dayIndex);
            
            return processPathDataResult;
        }

        // Original hourly logic for Day/Intraday modes
        const timeSlots = generateTimeSlots(params, startDateStr);
        
        if (timeSlots.length === 0) {
            return [];
        }

        // Batch fetch in groups of 12 for better parallelization
        const BATCH_SIZE = 12;
        for (let i = 0; i < timeSlots.length; i += BATCH_SIZE) {
            const batch = timeSlots.slice(i, i + BATCH_SIZE);
            
            const batchPromises = batch.map(async (slotData) => {
                const url = buildHourlyProcessPathURLWithMinutes(slotData.startDateStr, slotData.startHour, slotData.startMinute, slotData.endDateStr, slotData.endHour, slotData.endMinute);
                if (!url) return null;

                // Build time range label
                const startMinStr = String(slotData.startMinute).padStart(2, '0');
                const endHourDisplay = slotData.endHour === 0 && slotData.endMinute === 0 ? '24' : String(slotData.endHour).padStart(2, '0');
                const endMinStr = String(slotData.endMinute).padStart(2, '0');
                const timeRange = `${String(slotData.startHour).padStart(2, '0')}:${startMinStr}-${endHourDisplay}:${endMinStr}`;

                try {
                    const html = await fetchURL(url);
                    currentFetchStep++;
                    updateLoadingBar();

                    // OB Indirects
                    const obTotal = parseProcessPathData(html, 'ppr.detail.outbound.outbound.total');
                    const obLeadPA = parseProcessPathData(html, 'ppr.detail.outbound.obLeadPA.obLeadPA');
                    const obProblemSolve = parseProcessPathData(html, 'ppr.detail.outbound.obDefect.obProblemSolve');
                    const packSupport = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSupport');
                    const pickSupport = parseProcessPathData(html, 'ppr.detail.outbound.pick.pickSupport');
                    const shipDock = parseProcessPathData(html, 'ppr.detail.outbound.ship.shipDock');

                    // OB Direct
                    const pick = parseProcessPathData(html, 'ppr.detail.outbound.pick.pick.total');
                    const packSingle = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSingle.total');
                    const spp = parseProcessPathData(html, 'ppr.detail.outbound.pack.packLevAutofold');
                    const sort = parseProcessPathData(html, 'ppr.detail.outbound.sort.sort.total');
                    const chuting = parseProcessPathData(html, 'ppr.detail.outbound.pack.chuting.total');
                    const packMultis = parseProcessPathData(html, 'ppr.detail.outbound.pack.packMultis.total');

                    // Transfer Out (DA)
                    const daBldgTransfer = parseProcessPathData(html, 'ppr.detail.da.da.total');
                    const transferOutPick = parseProcessPathData(html, 'ppr.detail.da.rcPick.transferOutPick.total');
                    const transferOut = parseProcessPathData(html, 'ppr.detail.da.transferOut.transferOut');
                    const transferOutDock = parseProcessPathData(html, 'ppr.detail.da.transferOutDock.transferOutDock');
                    const toLeadPA = parseProcessPathData(html, 'ppr.detail.da.toLeadPa.toLeadPa');

                    // Vendor Returns
                    const vretTot = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vendorReturns.total');
                    const vretPick = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPick.total');
                    const vretPack = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPack.total');
                    const vretSupport = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnSupport');

                    // Department Overview
                    const throughputRaw = parseProcessPathData(html, 'ppr.fcSummary.throughput');
                    const supportSummary = parseProcessPathData(html, 'ppr.fcSummary.support');
                    const ibTot = parseProcessPathData(html, 'ppr.fcSummary.totalInbound');
                    const obTotSummary = parseProcessPathData(html, 'ppr.fcSummary.outbound');
                    const tsoTotSummary = parseProcessPathData(html, 'ppr.fcSummary.transferOut');
                    const revLogSummary = parseProcessPathData(html, 'ppr.fcSummary.reverseLogistics');
                    const icqaTot = parseProcessPathData(html, 'ppr.detail.support.support.ICQACS');

                    // Deduct Support varHrs from Throughput varHrs (Support hours are incorrectly included on the website)
                    const tpVarHrsRaw = normalizeNumber(throughputRaw.varHrs);
                    const supportVarHrs = normalizeNumber(supportSummary.varHrs);
                    const adjustedTpVarHrs = tpVarHrsRaw - supportVarHrs;
                    const throughput = {
                        vol: throughputRaw.vol,
                        hrs: throughputRaw.hrs,
                        rate: throughputRaw.rate,
                        planRate: throughputRaw.planRate,
                        varHrs: adjustedTpVarHrs.toString()
                    };

                    // IB Indirects
                    const ibTotal = parseProcessPathData(html, 'ppr.fcSummary.totalInbound');
                    const receiveDock = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveDock');
                    const receiveSupport = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveSupport');
                    const tsiSupport = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInSupport');
                    const stpSupport = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.stowToPrimeSupport');
                    const rsrSupport = parseProcessPathData(html, 'ppr.detail.inbound.rsr.rsrSupport');
                    const ibLeadPA = parseProcessPathData(html, 'ppr.detail.inbound.ibLeadPa.ibLeadPa');

                    // IB Direct
                    const tsiVol = parseProcessPathData(html, 'ppr.fcSummary.transferIn');
                    const nvfVol = parseProcessPathData(html, 'ppr.fcSummary.vendorRec');
                    const eachTSI = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.eachTransferIn.total');
                    const tsiDecant = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInDecant');
                    const eachStP = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.eachStowToPrime.total');
                    const prepRecorder = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.prepRecorder.total');
                    const cubiscan = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.cubiscan');
                    
                    return {
                        timeRange,
                        hour: slotData.startHour,
                        // Department Overview (FC Summary)
                        throughput,
                        ibTot,
                        obTotSummary,
                        tsoTotSummary,
                        revLogSummary,
                        icqaTot,
                        // OB Indirects
                        obVol: obTotal.vol,
                        obTot: obTotal,
                        obLeadPA,
                        obProblemSolve,
                        packSupport,
                        pickSupport,
                        shipDock,
                        // OB Direct
                        pick,
                        packSingle,
                        spp,
                        sort,
                        chuting,
                        packMultis,
                        // Transfer Out
                        daBldgTransferVol: daBldgTransfer.vol,
                        daBldgTransfer,
                        transferOutPick,
                        transferOut,
                        transferOutDock,
                        toLeadPA,
                        // Vendor Returns
                        vretVol: vretSupport.vol,
                        vretTot,
                        vretPick,
                        vretPack,
                        vretSupport,
                        // IB Indirects
                        ibTotal,
                        receiveDock,
                        receiveSupport,
                        tsiSupport,
                        stpSupport,
                        rsrSupport,
                        ibLeadPA,
                        // IB Direct
                        tsiVol,
                        nvfVol,
                        eachTSI,
                        tsiDecant,
                        eachStP,
                        prepRecorder,
                        cubiscan
                    };
                } catch (error) {
                    console.error(`❌ Error fetching process path slot ${timeRange}:`, error);
                    failedSlots.push(`Process Path ${timeRange}`);
                    currentFetchStep++;
                    updateLoadingBar();
                    return null;
                }
            });

            const batchResults = await Promise.all(batchPromises);
            batchResults.forEach(result => {
                if (result) processPathDataResult.push(result);
            });
        }

        return processPathDataResult;
    }

    async function fetchFullDayProcessPathTotals() {
        const params = getFormParameters();
        if (!params || !params.startDateStr) return null;

        let url;
        
        if (params.spanType === 'Week') {
            // Week mode: use Week spanType URL
            const warehouseId = getWarehouseId();
            if (!warehouseId) return null;
            url = `https://fclm-portal.amazon.com/reports/processPathRollup?warehouseId=${warehouseId}&spanType=Week&startDateWeek=${encodeURIComponent(params.startDateWeek)}`;
        } else if (params.spanType === 'Intraday') {
            // Use selected Intraday time range
            const startDateStr = params.startDateStr;
            const endDateStr = params.endDateStr || params.startDateStr;
            const startHour = params.startHourNum;
            const startMinute = params.startMinuteNum || 0;
            const endHour = params.endHourNum;
            const endMinute = params.endMinuteNum || 0;
            url = buildHourlyProcessPathURLWithMinutes(startDateStr, startHour, startMinute, endDateStr, endHour, endMinute);
        } else {
            // Day mode: full day (00:00 to 24:00)
            const startDateStr = params.startDateStr;
            const dateParts = startDateStr.split('-');
            const y = parseInt(dateParts[0]), m = parseInt(dateParts[1]) - 1, d = parseInt(dateParts[2]);
            const nextDayDate = new Date(y, m, d + 1);
            const endDateStr = nextDayDate.getFullYear() + '-' + String(nextDayDate.getMonth() + 1).padStart(2, '0') + '-' + String(nextDayDate.getDate()).padStart(2, '0');
            url = buildHourlyProcessPathURLWithMinutes(startDateStr, 0, 0, endDateStr, 0, 0);
        }

        if (!url) return null;

        try {
            const html = await fetchURL(url);
            currentFetchStep++;
            updateLoadingBar();

            // OB Indirects
            const obTotal = parseProcessPathData(html, 'ppr.detail.outbound.outbound.total');
            const obLeadPA = parseProcessPathData(html, 'ppr.detail.outbound.obLeadPA.obLeadPA');
            const obProblemSolve = parseProcessPathData(html, 'ppr.detail.outbound.obDefect.obProblemSolve');
            const packSupport = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSupport');
            const pickSupport = parseProcessPathData(html, 'ppr.detail.outbound.pick.pickSupport');
            const shipDock = parseProcessPathData(html, 'ppr.detail.outbound.ship.shipDock');

            // OB Direct
            const pick = parseProcessPathData(html, 'ppr.detail.outbound.pick.pick.total');
            const packSingle = parseProcessPathData(html, 'ppr.detail.outbound.pack.packSingle.total');
            const spp = parseProcessPathData(html, 'ppr.detail.outbound.pack.packLevAutofold');
            const sort = parseProcessPathData(html, 'ppr.detail.outbound.sort.sort.total');
            const chuting = parseProcessPathData(html, 'ppr.detail.outbound.pack.chuting.total');
            const packMultis = parseProcessPathData(html, 'ppr.detail.outbound.pack.packMultis.total');

            // Transfer Out
            const daBldgTransfer = parseProcessPathData(html, 'ppr.detail.da.da.total');
            const transferOutPick = parseProcessPathData(html, 'ppr.detail.da.rcPick.transferOutPick.total');
            const transferOut = parseProcessPathData(html, 'ppr.detail.da.transferOut.transferOut');
            const transferOutDock = parseProcessPathData(html, 'ppr.detail.da.transferOutDock.transferOutDock');
            const toLeadPA = parseProcessPathData(html, 'ppr.detail.da.toLeadPa.toLeadPa');

            // Vendor Returns
            const vretTot = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vendorReturns.total');
            const vretPick = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPick.total');
            const vretPack = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnPack.total');
            const vretSupport = parseProcessPathData(html, 'ppr.detail.reverseLogistics.vReturn.vReturnSupport');

            // IB Indirects
            const ibTotal = parseProcessPathData(html, 'ppr.fcSummary.totalInbound');
            const receiveDock = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveDock');
            const receiveSupport = parseProcessPathData(html, 'ppr.detail.inbound.receive.receiveSupport');
            const tsiSupport = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInSupport');
            const stpSupport = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.stowToPrimeSupport');
            const rsrSupport = parseProcessPathData(html, 'ppr.detail.inbound.rsr.rsrSupport');
            const ibLeadPA = parseProcessPathData(html, 'ppr.detail.inbound.ibLeadPa.ibLeadPa');

            // IB Direct
            const tsiVol = parseProcessPathData(html, 'ppr.fcSummary.transferIn');
            const nvfVol = parseProcessPathData(html, 'ppr.fcSummary.vendorRec');
            const eachTSI = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.eachTransferIn.total');
            const tsiDecant = parseProcessPathData(html, 'ppr.detail.inbound.transferInAndStow.transferInDecant');
            const eachStP = parseProcessPathData(html, 'ppr.detail.inbound.stowToPrime.eachStowToPrime.total');
            const prepRecorder = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.prepRecorder.total');
            const cubiscan = parseProcessPathData(html, 'ppr.detail.inbound.ibPrep.cubiscan');

            return {
                // OB Indirects
                obTot: obTotal,
                shipDock,
                packSupport,
                obLeadPA,
                obProblemSolve,
                pickSupport,
                // OB Direct
                pick,
                packSingle,
                spp,
                sort,
                chuting,
                packMultis,
                // Transfer Out
                daBldgTransfer,
                transferOutPick,
                transferOut,
                transferOutDock,
                toLeadPA,
                // Vendor Returns
                vretTot,
                vretPick,
                vretPack,
                vretSupport,
                // IB Indirects
                ibTotal,
                receiveDock,
                receiveSupport,
                tsiSupport,
                stpSupport,
                rsrSupport,
                ibLeadPA,
                // IB Direct
                tsiVol,
                nvfVol,
                eachTSI,
                tsiDecant,
                eachStP,
                prepRecorder,
                cubiscan
            };
        } catch (error) {
            console.error('❌ Error fetching PPR totals:', error);
            failedSlots.push('PPR Totals');
            currentFetchStep++;
            updateLoadingBar();
            return null;
        }
    }

    let fullDayPPRTotals = null;

    function updateProcessPathFooterTotals(totals) {
        if (!totals) return;

        function formatRateFn(value) {
            const num = normalizeNumber(value);
            return num.toFixed(1);
        }

        function formatVarHrsFn(value, isOBTot) {
            const num = normalizeNumber(value);
            const rounded = Math.round(num);
            const threshold = isOBTot ? 10 : 2;
            
            if (num < -threshold) {
                return `<span class="negative-diff">${rounded}</span>`;
            } else if (num > threshold) {
                return `<span class="positive-diff">${rounded}</span>`;
            }
            return `${rounded}`;
        }

        // OB Indirects Table
        // OB Tot
        if (document.getElementById('totalOBTotTPH')) document.getElementById('totalOBTotTPH').textContent = formatRateFn(totals.obTot.rate);
        if (document.getElementById('totalOBTotTarget')) document.getElementById('totalOBTotTarget').textContent = formatRateFn(totals.obTot.planRate);
        if (document.getElementById('totalOBTotVar')) document.getElementById('totalOBTotVar').innerHTML = formatVarHrsFn(totals.obTot.varHrs, true);

        // Ship Dock
        if (document.getElementById('totalShipDockTPH')) document.getElementById('totalShipDockTPH').textContent = formatRateFn(totals.shipDock.rate);
        if (document.getElementById('totalShipDockTarget')) document.getElementById('totalShipDockTarget').textContent = formatRateFn(totals.shipDock.planRate);
        if (document.getElementById('totalShipDockVar')) document.getElementById('totalShipDockVar').innerHTML = formatVarHrsFn(totals.shipDock.varHrs, false);

        // Pack Support
        if (document.getElementById('totalPackSuppTPH')) document.getElementById('totalPackSuppTPH').textContent = formatRateFn(totals.packSupport.rate);
        if (document.getElementById('totalPackSuppTarget')) document.getElementById('totalPackSuppTarget').textContent = formatRateFn(totals.packSupport.planRate);
        if (document.getElementById('totalPackSuppVar')) document.getElementById('totalPackSuppVar').innerHTML = formatVarHrsFn(totals.packSupport.varHrs, false);

        // OB Lead/PA
        if (document.getElementById('totalOBLeadTPH')) document.getElementById('totalOBLeadTPH').textContent = formatRateFn(totals.obLeadPA.rate);
        if (document.getElementById('totalOBLeadTarget')) document.getElementById('totalOBLeadTarget').textContent = formatRateFn(totals.obLeadPA.planRate);
        if (document.getElementById('totalOBLeadVar')) document.getElementById('totalOBLeadVar').innerHTML = formatVarHrsFn(totals.obLeadPA.varHrs, false);

        // OB PS
        if (document.getElementById('totalOBPSTPH')) document.getElementById('totalOBPSTPH').textContent = formatRateFn(totals.obProblemSolve.rate);
        if (document.getElementById('totalOBPSTarget')) document.getElementById('totalOBPSTarget').textContent = formatRateFn(totals.obProblemSolve.planRate);
        if (document.getElementById('totalOBPSVar')) document.getElementById('totalOBPSVar').innerHTML = formatVarHrsFn(totals.obProblemSolve.varHrs, false);

        // Pick Support
        if (document.getElementById('totalPickSuppTPH')) document.getElementById('totalPickSuppTPH').textContent = formatRateFn(totals.pickSupport.rate);
        if (document.getElementById('totalPickSuppTarget')) document.getElementById('totalPickSuppTarget').textContent = formatRateFn(totals.pickSupport.planRate);
        if (document.getElementById('totalPickSuppVar')) document.getElementById('totalPickSuppVar').innerHTML = formatVarHrsFn(totals.pickSupport.varHrs, false);

        // OB Direct Table
        // Pick
        if (document.getElementById('totalPickTPH')) document.getElementById('totalPickTPH').textContent = formatRateFn(totals.pick.rate);
        if (document.getElementById('totalPickTarget')) document.getElementById('totalPickTarget').textContent = formatRateFn(totals.pick.planRate);
        if (document.getElementById('totalPickVar')) document.getElementById('totalPickVar').innerHTML = formatVarHrsFn(totals.pick.varHrs, false);

        // Pack Single
        if (document.getElementById('totalPackSiTPH')) document.getElementById('totalPackSiTPH').textContent = formatRateFn(totals.packSingle.rate);
        if (document.getElementById('totalPackSiTarget')) document.getElementById('totalPackSiTarget').textContent = formatRateFn(totals.packSingle.planRate);
        if (document.getElementById('totalPackSiVar')) document.getElementById('totalPackSiVar').innerHTML = formatVarHrsFn(totals.packSingle.varHrs, false);

        // SPP
        if (document.getElementById('totalSPPTPH')) document.getElementById('totalSPPTPH').textContent = formatRateFn(totals.spp.rate);
        if (document.getElementById('totalSPPTarget')) document.getElementById('totalSPPTarget').textContent = formatRateFn(totals.spp.planRate);
        if (document.getElementById('totalSPPVar')) document.getElementById('totalSPPVar').innerHTML = formatVarHrsFn(totals.spp.varHrs, false);

        // Sort
        if (document.getElementById('totalSortTPH')) document.getElementById('totalSortTPH').textContent = formatRateFn(totals.sort.rate);
        if (document.getElementById('totalSortTarget')) document.getElementById('totalSortTarget').textContent = formatRateFn(totals.sort.planRate);
        if (document.getElementById('totalSortVar')) document.getElementById('totalSortVar').innerHTML = formatVarHrsFn(totals.sort.varHrs, false);

        // Chuting
        if (document.getElementById('totalChutingTPH')) document.getElementById('totalChutingTPH').textContent = formatRateFn(totals.chuting.rate);
        if (document.getElementById('totalChutingTarget')) document.getElementById('totalChutingTarget').textContent = formatRateFn(totals.chuting.planRate);
        if (document.getElementById('totalChutingVar')) document.getElementById('totalChutingVar').innerHTML = formatVarHrsFn(totals.chuting.varHrs, false);

        // Pack Multis
        if (document.getElementById('totalPackMuTPH')) document.getElementById('totalPackMuTPH').textContent = formatRateFn(totals.packMultis.rate);
        if (document.getElementById('totalPackMuTarget')) document.getElementById('totalPackMuTarget').textContent = formatRateFn(totals.packMultis.planRate);
        if (document.getElementById('totalPackMuVar')) document.getElementById('totalPackMuVar').innerHTML = formatVarHrsFn(totals.packMultis.varHrs, false);

        // Transfer Out Table
        // DA Bldg Transfer (Transfer Out Total)
        if (document.getElementById('totalDABldgTPH')) document.getElementById('totalDABldgTPH').textContent = formatRateFn(totals.daBldgTransfer.rate);
        if (document.getElementById('totalDABldgTarget')) document.getElementById('totalDABldgTarget').textContent = formatRateFn(totals.daBldgTransfer.planRate);
        if (document.getElementById('totalDABldgVar')) document.getElementById('totalDABldgVar').innerHTML = formatVarHrsFn(totals.daBldgTransfer.varHrs, false);

        // Transfer Out Pick
        if (document.getElementById('totalTOPickTPH')) document.getElementById('totalTOPickTPH').textContent = formatRateFn(totals.transferOutPick?.rate);
        if (document.getElementById('totalTOPickTarget')) document.getElementById('totalTOPickTarget').textContent = formatRateFn(totals.transferOutPick?.planRate);
        if (document.getElementById('totalTOPickVar')) document.getElementById('totalTOPickVar').innerHTML = formatVarHrsFn(totals.transferOutPick?.varHrs, false);

        // Transfer Out
        if (document.getElementById('totalTransOutTPH')) document.getElementById('totalTransOutTPH').textContent = formatRateFn(totals.transferOut.rate);
        if (document.getElementById('totalTransOutTarget')) document.getElementById('totalTransOutTarget').textContent = formatRateFn(totals.transferOut.planRate);
        if (document.getElementById('totalTransOutVar')) document.getElementById('totalTransOutVar').innerHTML = formatVarHrsFn(totals.transferOut.varHrs, false);

        // Transfer Out Dock
        if (document.getElementById('totalTransDockTPH')) document.getElementById('totalTransDockTPH').textContent = formatRateFn(totals.transferOutDock.rate);
        if (document.getElementById('totalTransDockTarget')) document.getElementById('totalTransDockTarget').textContent = formatRateFn(totals.transferOutDock.planRate);
        if (document.getElementById('totalTransDockVar')) document.getElementById('totalTransDockVar').innerHTML = formatVarHrsFn(totals.transferOutDock.varHrs, false);

        // TO Lead/PA
        if (document.getElementById('totalTOLeadTPH')) document.getElementById('totalTOLeadTPH').textContent = formatRateFn(totals.toLeadPA.rate);
        if (document.getElementById('totalTOLeadTarget')) document.getElementById('totalTOLeadTarget').textContent = formatRateFn(totals.toLeadPA.planRate);
        if (document.getElementById('totalTOLeadVar')) document.getElementById('totalTOLeadVar').innerHTML = formatVarHrsFn(totals.toLeadPA.varHrs, false);

        // Vendor Returns Table
        // VRET Tot
        if (document.getElementById('totalVRETTotTPH')) document.getElementById('totalVRETTotTPH').textContent = formatRateFn(totals.vretTot.rate);
        if (document.getElementById('totalVRETTotTarget')) document.getElementById('totalVRETTotTarget').textContent = formatRateFn(totals.vretTot.planRate);
        if (document.getElementById('totalVRETTotVar')) document.getElementById('totalVRETTotVar').innerHTML = formatVarHrsFn(totals.vretTot.varHrs, false);

        // VRET Pick
        if (document.getElementById('totalVRETPickTPH')) document.getElementById('totalVRETPickTPH').textContent = formatRateFn(totals.vretPick.rate);
        if (document.getElementById('totalVRETPickTarget')) document.getElementById('totalVRETPickTarget').textContent = formatRateFn(totals.vretPick.planRate);
        if (document.getElementById('totalVRETPickVar')) document.getElementById('totalVRETPickVar').innerHTML = formatVarHrsFn(totals.vretPick.varHrs, false);

        // VRET Pack
        if (document.getElementById('totalVRETPackTPH')) document.getElementById('totalVRETPackTPH').textContent = formatRateFn(totals.vretPack.rate);
        if (document.getElementById('totalVRETPackTarget')) document.getElementById('totalVRETPackTarget').textContent = formatRateFn(totals.vretPack.planRate);
        if (document.getElementById('totalVRETPackVar')) document.getElementById('totalVRETPackVar').innerHTML = formatVarHrsFn(totals.vretPack.varHrs, false);

        // VRET Support
        if (document.getElementById('totalVRETSuppTPH')) document.getElementById('totalVRETSuppTPH').textContent = formatRateFn(totals.vretSupport.rate);
        if (document.getElementById('totalVRETSuppTarget')) document.getElementById('totalVRETSuppTarget').textContent = formatRateFn(totals.vretSupport.planRate);
        if (document.getElementById('totalVRETSuppVar')) document.getElementById('totalVRETSuppVar').innerHTML = formatVarHrsFn(totals.vretSupport.varHrs, false);

        // IB Indirects Table
        // IB Tot
        if (document.getElementById('totalIBTotTPH')) document.getElementById('totalIBTotTPH').textContent = formatRateFn(totals.ibTotal.rate);
        if (document.getElementById('totalIBTotTarget')) document.getElementById('totalIBTotTarget').textContent = formatRateFn(totals.ibTotal.planRate);
        if (document.getElementById('totalIBTotVarHrs')) document.getElementById('totalIBTotVarHrs').innerHTML = formatVarHrsFn(totals.ibTotal.varHrs, true);

        // Receive Dock
        if (document.getElementById('totalRecDockTPH')) document.getElementById('totalRecDockTPH').textContent = formatRateFn(totals.receiveDock.rate);
        if (document.getElementById('totalRecDockTarget')) document.getElementById('totalRecDockTarget').textContent = formatRateFn(totals.receiveDock.planRate);
        if (document.getElementById('totalRecDockVar')) document.getElementById('totalRecDockVar').innerHTML = formatVarHrsFn(totals.receiveDock.varHrs, false);

        // Receive Support
        if (document.getElementById('totalRecSuppTPH')) document.getElementById('totalRecSuppTPH').textContent = formatRateFn(totals.receiveSupport.rate);
        if (document.getElementById('totalRecSuppTarget')) document.getElementById('totalRecSuppTarget').textContent = formatRateFn(totals.receiveSupport.planRate);
        if (document.getElementById('totalRecSuppVar')) document.getElementById('totalRecSuppVar').innerHTML = formatVarHrsFn(totals.receiveSupport.varHrs, false);

        // TSI Support
        if (document.getElementById('totalTSISuppTPH')) document.getElementById('totalTSISuppTPH').textContent = formatRateFn(totals.tsiSupport.rate);
        if (document.getElementById('totalTSISuppTarget')) document.getElementById('totalTSISuppTarget').textContent = formatRateFn(totals.tsiSupport.planRate);
        if (document.getElementById('totalTSISuppVar')) document.getElementById('totalTSISuppVar').innerHTML = formatVarHrsFn(totals.tsiSupport.varHrs, false);

        // StP Support
        if (document.getElementById('totalStPSuppTPH')) document.getElementById('totalStPSuppTPH').textContent = formatRateFn(totals.stpSupport.rate);
        if (document.getElementById('totalStPSuppTarget')) document.getElementById('totalStPSuppTarget').textContent = formatRateFn(totals.stpSupport.planRate);
        if (document.getElementById('totalStPSuppVar')) document.getElementById('totalStPSuppVar').innerHTML = formatVarHrsFn(totals.stpSupport.varHrs, false);

        // RSR Support
        if (document.getElementById('totalRSRSuppTPH')) document.getElementById('totalRSRSuppTPH').textContent = formatRateFn(totals.rsrSupport.rate);
        if (document.getElementById('totalRSRSuppTarget')) document.getElementById('totalRSRSuppTarget').textContent = formatRateFn(totals.rsrSupport.planRate);
        if (document.getElementById('totalRSRSuppVar')) document.getElementById('totalRSRSuppVar').innerHTML = formatVarHrsFn(totals.rsrSupport.varHrs, false);

        // IB Lead/PA
        if (document.getElementById('totalIBLeadTPH')) document.getElementById('totalIBLeadTPH').textContent = formatRateFn(totals.ibLeadPA.rate);
        if (document.getElementById('totalIBLeadTarget')) document.getElementById('totalIBLeadTarget').textContent = formatRateFn(totals.ibLeadPA.planRate);
        if (document.getElementById('totalIBLeadVar')) document.getElementById('totalIBLeadVar').innerHTML = formatVarHrsFn(totals.ibLeadPA.varHrs, false);

        // IB Direct Table
        // Each TSI
        if (document.getElementById('totalEachTSITPH')) document.getElementById('totalEachTSITPH').textContent = formatRateFn(totals.eachTSI.rate);
        if (document.getElementById('totalEachTSITarget')) document.getElementById('totalEachTSITarget').textContent = formatRateFn(totals.eachTSI.planRate);
        if (document.getElementById('totalEachTSIVar')) document.getElementById('totalEachTSIVar').innerHTML = formatVarHrsFn(totals.eachTSI.varHrs, false);

        // TSI Decant
        if (document.getElementById('totalTSIDecantTPH')) document.getElementById('totalTSIDecantTPH').textContent = formatRateFn(totals.tsiDecant.rate);
        if (document.getElementById('totalTSIDecantTarget')) document.getElementById('totalTSIDecantTarget').textContent = formatRateFn(totals.tsiDecant.planRate);
        if (document.getElementById('totalTSIDecantVar')) document.getElementById('totalTSIDecantVar').innerHTML = formatVarHrsFn(totals.tsiDecant.varHrs, false);

        // Each StP
        if (document.getElementById('totalEachStPTPH')) document.getElementById('totalEachStPTPH').textContent = formatRateFn(totals.eachStP.rate);
        if (document.getElementById('totalEachStPTarget')) document.getElementById('totalEachStPTarget').textContent = formatRateFn(totals.eachStP.planRate);
        if (document.getElementById('totalEachStPVar')) document.getElementById('totalEachStPVar').innerHTML = formatVarHrsFn(totals.eachStP.varHrs, false);

        // Prep Recorder
        if (document.getElementById('totalPrepRecTPH')) document.getElementById('totalPrepRecTPH').textContent = formatRateFn(totals.prepRecorder.rate);
        if (document.getElementById('totalPrepRecTarget')) document.getElementById('totalPrepRecTarget').textContent = formatRateFn(totals.prepRecorder.planRate);
        if (document.getElementById('totalPrepRecVar')) document.getElementById('totalPrepRecVar').innerHTML = formatVarHrsFn(totals.prepRecorder.varHrs, false);

        // Cubiscan
        if (document.getElementById('totalCubiscanTPH')) document.getElementById('totalCubiscanTPH').textContent = formatRateFn(totals.cubiscan.rate);
        if (document.getElementById('totalCubiscanTarget')) document.getElementById('totalCubiscanTarget').textContent = formatRateFn(totals.cubiscan.planRate);
        if (document.getElementById('totalCubiscanVar')) document.getElementById('totalCubiscanVar').innerHTML = formatVarHrsFn(totals.cubiscan.varHrs, false);

        // Re-apply manual target overrides (they take precedence over PPR full-day totals)
        reapplyAllFooterOverrides();
    }

    // Re-apply manual target overrides to all footer totals
    // Called after updateProcessPathFooterTotals to ensure overrides aren't lost
    function reapplyAllFooterOverrides() {
        if (!processPathData || processPathData.length === 0) return;
        const data = processPathData;
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];

        // OB Indirects
        updateProcessFooterWithOverride(data, 'obTot', 'totalOBTotTPH', 'totalOBTotTarget', 'totalOBTotVar', tr => getTarget(tr, 'obTot'), true);
        updateProcessFooterWithOverride(data, 'shipDock', 'totalShipDockTPH', 'totalShipDockTarget', 'totalShipDockVar', tr => getTarget(tr, 'shipDock'));
        updateProcessFooterWithOverride(data, 'packSupport', 'totalPackSuppTPH', 'totalPackSuppTarget', 'totalPackSuppVar', tr => getTarget(tr, 'packSupport'));
        updateProcessFooterWithOverride(data, 'obLeadPA', 'totalOBLeadTPH', 'totalOBLeadTarget', 'totalOBLeadVar', tr => getTarget(tr, 'obLeadPA'));
        updateProcessFooterWithOverride(data, 'obProblemSolve', 'totalOBPSTPH', 'totalOBPSTarget', 'totalOBPSVar', tr => getTarget(tr, 'obProblemSolve'));
        updateProcessFooterWithOverride(data, 'pickSupport', 'totalPickSuppTPH', 'totalPickSuppTarget', 'totalPickSuppVar', tr => getTarget(tr, 'pickSupport'));

        // OB Direct
        updateProcessFooterWithOverride(data, 'pick', 'totalPickTPH', 'totalPickTarget', 'totalPickVar', tr => getTarget(tr, 'pick'));
        updateProcessFooterWithOverride(data, 'packSingle', 'totalPackSiTPH', 'totalPackSiTarget', 'totalPackSiVar', tr => getTarget(tr, 'packSingle'));
        updateProcessFooterWithOverride(data, 'spp', 'totalSPPTPH', 'totalSPPTarget', 'totalSPPVar', tr => getTarget(tr, 'spp'));
        updateProcessFooterWithOverride(data, 'sort', 'totalSortTPH', 'totalSortTarget', 'totalSortVar', tr => getTarget(tr, 'sort'));
        updateProcessFooterWithOverride(data, 'chuting', 'totalChutingTPH', 'totalChutingTarget', 'totalChutingVar', tr => getTarget(tr, 'chuting'));
        updateProcessFooterWithOverride(data, 'packMultis', 'totalPackMuTPH', 'totalPackMuTarget', 'totalPackMuVar', tr => getTarget(tr, 'packMultis'));

        // IB Indirects
        const ibTotData = data.map(h => ({ ...h, ibTot: h.ibTotal || h.ibTot || {} }));
        updateProcessFooterWithOverride(ibTotData, 'ibTot', 'totalIBTotTPH', 'totalIBTotTarget', 'totalIBTotVar', tr => getTarget(tr, 'ibTot'), true);
        updateProcessFooterWithOverride(data, 'receiveDock', 'totalRecDockTPH', 'totalRecDockTarget', 'totalRecDockVar', tr => getTarget(tr, 'receiveDock'));
        updateProcessFooterWithOverride(data, 'receiveSupport', 'totalRecSuppTPH', 'totalRecSuppTarget', 'totalRecSuppVar', tr => getTarget(tr, 'receiveSupport'));
        updateProcessFooterWithOverride(data, 'tsiSupport', 'totalTSISuppTPH', 'totalTSISuppTarget', 'totalTSISuppVar', tr => getTarget(tr, 'tsiSupport'));
        updateProcessFooterWithOverride(data, 'stpSupport', 'totalStPSuppTPH', 'totalStPSuppTarget', 'totalStPSuppVar', tr => getTarget(tr, 'stpSupport'));
        updateProcessFooterWithOverride(data, 'rsrSupport', 'totalRSRSuppTPH', 'totalRSRSuppTarget', 'totalRSRSuppVar', tr => getTarget(tr, 'rsrSupport'));
        updateProcessFooterWithOverride(data, 'ibLeadPA', 'totalIBLeadTPH', 'totalIBLeadTarget', 'totalIBLeadVar', tr => getTarget(tr, 'ibLeadPA'));

        // IB Direct
        updateProcessFooterWithOverride(data, 'eachTSI', 'totalEachTSITPH', 'totalEachTSITarget', 'totalEachTSIVar', tr => getTarget(tr, 'eachTSI'));
        updateProcessFooterWithOverride(data, 'tsiDecant', 'totalTSIDecantTPH', 'totalTSIDecantTarget', 'totalTSIDecantVar', tr => getTarget(tr, 'tsiDecant'));
        updateProcessFooterWithOverride(data, 'eachStP', 'totalEachStPTPH', 'totalEachStPTarget', 'totalEachStPVar', tr => getTarget(tr, 'eachStP'));
        updateProcessFooterWithOverride(data, 'prepRecorder', 'totalPrepRecTPH', 'totalPrepRecTarget', 'totalPrepRecVar', tr => getTarget(tr, 'prepRecorder'));
        updateProcessFooterWithOverride(data, 'cubiscan', 'totalCubiscanTPH', 'totalCubiscanTarget', 'totalCubiscanVar', tr => getTarget(tr, 'cubiscan'));

        // Transfer Out
        updateProcessFooterWithOverride(data, 'daBldgTransfer', 'totalDABldgTPH', 'totalDABldgTarget', 'totalDABldgVar', tr => getTarget(tr, 'daBldgTransfer'));
        updateProcessFooterWithOverride(data, 'transferOutPick', 'totalTOPickTPH', 'totalTOPickTarget', 'totalTOPickVar', tr => getTarget(tr, 'transferOutPick'));
        updateProcessFooterWithOverride(data, 'transferOut', 'totalTransOutTPH', 'totalTransOutTarget', 'totalTransOutVar', tr => getTarget(tr, 'transferOut'));
        updateProcessFooterWithOverride(data, 'transferOutDock', 'totalTransDockTPH', 'totalTransDockTarget', 'totalTransDockVar', tr => getTarget(tr, 'transferOutDock'));
        updateProcessFooterWithOverride(data, 'toLeadPA', 'totalTOLeadTPH', 'totalTOLeadTarget', 'totalTOLeadVar', tr => getTarget(tr, 'toLeadPA'));

        // Vendor Returns
        updateProcessFooterWithOverride(data, 'vretTot', 'totalVRETTotTPH', 'totalVRETTotTarget', 'totalVRETTotVar', tr => getTarget(tr, 'vretTot'));
        updateProcessFooterWithOverride(data, 'vretPick', 'totalVRETPickTPH', 'totalVRETPickTarget', 'totalVRETPickVar', tr => getTarget(tr, 'vretPick'));
        updateProcessFooterWithOverride(data, 'vretPack', 'totalVRETPackTPH', 'totalVRETPackTarget', 'totalVRETPackVar', tr => getTarget(tr, 'vretPack'));
        updateProcessFooterWithOverride(data, 'vretSupport', 'totalVRETSuppTPH', 'totalVRETSuppTarget', 'totalVRETSuppVar', tr => getTarget(tr, 'vretSupport'));
    }

    function loadTargets() {
        const savedSummary = localStorage.getItem('indirektSummaryTargets');
        const savedHourlyIndirect = localStorage.getItem('indirektHourlyIndirectTargets');
        const savedHourlyProcessPath = localStorage.getItem('indirektHourlyProcessPathTargets');

        if (savedSummary) {
            summaryTargets = JSON.parse(savedSummary);
        }

        if (savedHourlyIndirect) {
            hourlyIndirectTargets = JSON.parse(savedHourlyIndirect);
        }

        if (savedHourlyProcessPath) {
            hourlyProcessPathTargets = JSON.parse(savedHourlyProcessPath);
        }
    }

    function autoSaveTargets() {
        localStorage.setItem('indirektSummaryTargets', JSON.stringify(summaryTargets));
        localStorage.setItem('indirektHourlyIndirectTargets', JSON.stringify(hourlyIndirectTargets));
        localStorage.setItem('indirektHourlyProcessPathTargets', JSON.stringify(hourlyProcessPathTargets));
    }

    function toggleCategory(categoryId) {
        const rows = document.querySelectorAll('[data-category="' + categoryId + '"]');
        const icon = document.getElementById('icon-' + categoryId);

        if (!rows.length || !icon) return;

        const isVisible = rows[0].style.display !== 'none';

        rows.forEach(row => {
            row.style.setProperty('display', isVisible ? 'none' : 'table-row', 'important');

            if (isVisible) {
                const managerIcon = row.querySelector('.expand-icon[data-manager-id]');
                if (managerIcon) {
                    managerIcon.textContent = '▼';
                }
            }
        });

        if (isVisible) {
            const employeeRows = document.querySelectorAll('tr.employee-row[data-manager^="' + categoryId + '-"]');
            employeeRows.forEach(empRow => {
                empRow.style.setProperty('display', 'none', 'important');
            });
        }

        icon.textContent = isVisible ? '▼' : '▲';
    }

    function toggleManager(categoryId, managerId) {
        const rows = document.querySelectorAll('[data-manager="' + categoryId + '-' + managerId + '"]');
        const icon = document.getElementById('icon-' + categoryId + '-' + managerId);

        if (!rows.length || !icon) return;

        const isVisible = rows[0].style.display !== 'none';

        rows.forEach(row => {
            row.style.setProperty('display', isVisible ? 'none' : 'table-row', 'important');
        });

        icon.textContent = isVisible ? '▼' : '▲';
    }

    function attachSummaryEventListeners() {
        const tbody = document.getElementById('summaryTableBody');
        if (!tbody) return;

        tbody.removeEventListener('click', handleSummaryClick);
        tbody.addEventListener('click', handleSummaryClick);
    }

    function handleSummaryClick(e) {
        // Find the clicked row
        const row = e.target.closest('tr');
        if (!row) return;

        // Check if it's a category row (has expand icon with data-category-id)
        const categoryIcon = row.querySelector('.expand-icon[data-category-id]');
        if (categoryIcon) {
            const categoryId = categoryIcon.getAttribute('data-category-id');
            if (categoryId) {
                toggleCategory(categoryId);
                return;
            }
        }

        // Check if it's a manager row (has expand icon with data-manager-id)
        const managerIcon = row.querySelector('.expand-icon[data-manager-id]');
        if (managerIcon) {
            const managerId = managerIcon.getAttribute('data-manager-id');
            if (managerId) {
                const [catId, mgrId] = managerId.split('-mgr');
                toggleManager(catId, 'mgr' + mgrId);
                return;
            }
        }
    }

    function renderSummaryTable(data) {
        const tbody = document.getElementById('summaryTableBody');
        if (!tbody) return;

        // Add styles for clickable rows if not already present
        if (!document.getElementById('summaryTableStyles')) {
            const style = document.createElement('style');
            style.id = 'summaryTableStyles';
            style.textContent = '\
                .summary-table tbody tr { cursor: pointer; }\
                .summary-table tbody tr:hover { background: #e8f4fc !important; }\
                .summary-table .category-name { font-weight: 600; }\
                .summary-table .manager-name { padding-left: 20px; }\
                .summary-table .employee-row td:first-child { padding-left: 40px; }\
                .summary-table .employee-count { color: #666; font-size: 12px; }\
            ';
            document.head.appendChild(style);
        }

        if (data) {
            summaryData = data;
        } else if (!summaryData) {
            return;
        } else {
            data = summaryData;
        }

        let html = '';

        // Get bucket configuration for dynamic rendering
        const bucketConfig = getBucketConfig();
        const categories = bucketConfig.buckets.map(bucket => ({
            id: bucket.id,
            name: bucket.name
        }));

        let totalHours = 0;

        categories.forEach(cat => {
            const categoryData = employeeData[cat.id] || [];
            const hours = normalizeNumber(data[cat.id]);
            totalHours += hours;
            
            const managerGroups = {};
            categoryData.forEach(emp => {
                if (!managerGroups[emp.manager]) {
                    managerGroups[emp.manager] = [];
                }
                managerGroups[emp.manager].push(emp);
            });

            html += `<tr>
                <td>
                    <span class="expand-icon" id="icon-${cat.id}" data-category-id="${cat.id}">▼</span>
                    <span class="category-name">${cat.name}</span>
                </td>
                <td class="hours-value">${hours.toFixed(1)}</td>
            </tr>`;

            // Sort managers by total hours descending
            const sortedManagers = Object.keys(managerGroups).map(manager => {
                const employees = managerGroups[manager];
                const totalHours = employees.reduce((sum, emp) => sum + normalizeNumber(emp.hours), 0);
                return { manager, employees, totalHours };
            }).sort((a, b) => b.totalHours - a.totalHours);

            sortedManagers.forEach((managerData, idx) => {
                const { manager, employees, totalHours } = managerData;
                const managerHours = totalHours.toFixed(1);
                const managerId = `mgr${idx}`;

                html += `<tr class="manager-row" data-category="${cat.id}" style="display:none;">
                    <td>
                        <span class="expand-icon" id="icon-${cat.id}-${managerId}" data-manager-id="${cat.id}-${managerId}">▼</span>
                        <span class="manager-name">${manager}</span>
                        <span class="employee-count">(${employees.length})</span>
                    </td>
                    <td class="hours-value">${managerHours}</td>
                </tr>`;

                // Sort employees by hours descending
                const sortedEmployees = [...employees].sort((a, b) => normalizeNumber(b.hours) - normalizeNumber(a.hours));

                sortedEmployees.forEach(emp => {
                    const employeeUrl = buildEmployeeTimeDetailsURL(emp.id);
                    const empHours = normalizeNumber(emp.hours);
                    html += `<tr class="employee-row" data-manager="${cat.id}-${managerId}" style="display:none;">
                        <td>
                            ${employeeUrl ? `<a href="${employeeUrl}" target="_blank" class="employee-link">${emp.name} (${emp.id})</a>` : `${emp.name} (${emp.id})`}
                        </td>
                        <td class="hours-value">${empHours.toFixed(1)}</td>
                    </tr>`;
                });
            });
        });

        tbody.innerHTML = html;

        const summaryTotalCell = document.getElementById('summaryTotal');
        
        // Force reflow to prevent rendering issues
        forceReflow(tbody);
        if (summaryTotalCell) {
            summaryTotalCell.textContent = totalHours.toFixed(1);
        }

        attachSummaryEventListeners();
    }

    function renderHourlyTable(data) {
        const tbody = document.getElementById('hourlyTableBody');
        if (!tbody) return;

        if (data && data.length > 0) {
            hourlyData = data;
        } else if (!hourlyData || hourlyData.length === 0) {
            console.warn('⚠️ No hourly data to render');
            return;
        } else {
            data = hourlyData;
        }

        const params = getFormParameters();
        const isWeekMode = params?.spanType === 'Week';
        
        // Get bucket configuration for dynamic rendering
        const bucketConfig = getBucketConfig();
        const buckets = bucketConfig.buckets;

        // Update table headers dynamically
        updateIndirectTableHeaders(buckets, isWeekMode);

        let html = '';

        // Initialize totals dynamically based on buckets
        const totals = {};
        const targetTotals = {};
        buckets.forEach(bucket => {
            totals[bucket.id] = 0;
            targetTotals[bucket.id] = 0;
        });

        data.forEach(hourData => {
            // Accumulate totals for each bucket
            buckets.forEach(bucket => {
                const hrs = normalizeNumber(hourData[bucket.id]);
                totals[bucket.id] += hrs;
            });

            const targetKey = hourData.timeRange;
            const savedTarget = hourlyIndirectTargets[targetKey] || {};
            
            // Build target object dynamically
            const target = {};
            buckets.forEach(bucket => {
                target[bucket.id] = savedTarget[bucket.id] !== undefined ? savedTarget[bucket.id] : 0;
                targetTotals[bucket.id] += target[bucket.id];
            });

            function formatHrs(value) {
                const num = normalizeNumber(value);
                return isWeekMode ? Math.round(num) : num.toFixed(1);
            }

            function calcDiff(actual, target) {
                const a = normalizeNumber(actual);
                const t = normalizeNumber(target);
                const diff = t - a;
                
                if (diff === 0) {
                    return isWeekMode ? '0' : '0.0';
                }
                
                const diffClass = diff > 0 ? 'positive-diff' : 'negative-diff';
                return isWeekMode 
                    ? `<span class="${diffClass}">${diff > 0 ? '+' : ''}${Math.round(diff)}</span>`
                    : `<span class="${diffClass}">${diff > 0 ? '+' : ''}${diff.toFixed(1)}</span>`;
            }

            // Parse time range for links
            let startHour = 0;
            let endHour = 0;
            const slotDateStr = hourData.dateStr || (params ? params.startDateStr : '');
            let endDateStr = slotDateStr;
            
            if (!isWeekMode && hourData.timeRange.includes('-')) {
                const [startTime, endTime] = hourData.timeRange.split('-');
                startHour = parseInt(startTime.split(':')[0]) || 0;
                endHour = parseInt(endTime.split(':')[0]) || 0;
                
                if (endHour === 24 || endHour === 0) {
                    endHour = 0;
                    if (slotDateStr) {
                        const nextDay = new Date(slotDateStr);
                        nextDay.setDate(nextDay.getDate() + 1);
                        endDateStr = nextDay.toISOString().split('T')[0];
                    }
                }
            }

            // Get process ID for a bucket based on its first function's process
            function getProcessIdForBucket(bucket) {
                if (bucket.functions && bucket.functions.length > 0) {
                    const processKey = bucket.functions[0].process;
                    const processMap = { admin: '01002960', facilities: '01003028', nonfc: '01003047' };
                    return processMap[processKey] || '01002960';
                }
                return '01002960';
            }

            function buildFunctionRollupLink(processId, value) {
                const formattedValue = formatHrs(value);
                if (!slotDateStr && !hourData.dateStr) return formattedValue;
                
                if (isWeekMode) {
                    const url = buildDailyURL(processId, hourData.dateStr);
                    return `<a href="${url}" target="_blank" style="color: #0066c0; text-decoration: none;">${formattedValue}</a>`;
                }
                
                const url = buildHourlyURL(processId, slotDateStr, startHour, endDateStr, endHour);
                return `<a href="${url}" target="_blank" style="color: #0066c0; text-decoration: none;">${formattedValue}</a>`;
            }

            // Build row dynamically
            let rowHtml = `<tr><td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>`;
            
            buckets.forEach(bucket => {
                const processId = getProcessIdForBucket(bucket);
                const hrs = hourData[bucket.id] || 0;
                const tgt = target[bucket.id] || 0;
                
                rowHtml += `<td>${buildFunctionRollupLink(processId, hrs)}</td>`;
                rowHtml += `<td>${formatHrs(tgt)}</td>`;
                rowHtml += `<td>${calcDiff(hrs, tgt)}</td>`;
            });
            
            rowHtml += '</tr>';
            html += rowHtml;
        });

        tbody.innerHTML = html;

        // Update footer totals dynamically
        function formatTotalDiff(actual, target) {
            const diff = target - actual;
            if (diff === 0) {
                return isWeekMode ? '0' : '0.0';
            }
            const diffClass = diff > 0 ? 'positive-diff' : 'negative-diff';
            return isWeekMode 
                ? `<span class="${diffClass}">${diff > 0 ? '+' : ''}${Math.round(diff)}</span>`
                : `<span class="${diffClass}">${diff > 0 ? '+' : ''}${diff.toFixed(1)}</span>`;
        }

        function formatTotal(value) {
            return isWeekMode ? Math.round(value) : value.toFixed(1);
        }

        // Update footer - find tfoot and rebuild it dynamically
        const table = tbody.closest('table');
        let tfoot = table?.querySelector('tfoot');
        if (tfoot) {
            let footerHtml = '<tr><td>TOTAL</td>';
            buckets.forEach(bucket => {
                footerHtml += `<td>${formatTotal(totals[bucket.id])}</td>`;
                footerHtml += `<td>${formatTotal(targetTotals[bucket.id])}</td>`;
                footerHtml += `<td>${formatTotalDiff(totals[bucket.id], targetTotals[bucket.id])}</td>`;
            });
            footerHtml += '</tr>';
            tfoot.innerHTML = footerHtml;
        }
    }

    // Helper function to update indirect table headers dynamically
    function updateIndirectTableHeaders(buckets, isWeekMode) {
        const thead = document.getElementById('indirectTableHead');
        if (!thead) return;
        
        let row1 = '<tr><th rowspan="2">Time</th>';
        let row2 = '<tr>';
        
        buckets.forEach(bucket => {
            row1 += `<th colspan="3">${bucket.name}</th>`;
            row2 += '<th>Hrs</th><th>Target</th><th>Diff</th>';
        });
        
        row1 += '</tr>';
        row2 += '</tr>';
        
        thead.innerHTML = row1 + row2;
    }

    function renderProcessPathTable(data) {
        if (data && data.length > 0) {
            processPathData = data;
        } else if (!processPathData || processPathData.length === 0) {
            console.warn('⚠️ No process path data to render');
            return;
        } else {
            data = processPathData;
        }

        // Render OB tables (always render, visibility controlled by CSS)
        renderOBIndirectsTable(data);
        renderOBDirectTable(data);
        renderTransferOutTable(data);
        renderVendorReturnsTable(data);
        
        // Render IB tables (always render, visibility controlled by CSS)
        renderIBIndirectsTable(data);
        renderIBDirectTable(data);
    }

    // Helper functions for all tables
    function parseVol(value) {
        if (typeof value === 'number') return value;
        if (typeof value === 'string') {
            return normalizeNumber(value);
        }
        return 0;
    }

    function formatRate(value) {
        const num = normalizeNumber(value);
        return num.toFixed(1);
    }

    function formatHrsRound(value) {
        const num = normalizeNumber(value);
        return Math.round(num);
    }

    function formatVarHrs(value, isOBTot) {
        const num = normalizeNumber(value);
        const rounded = Math.round(num);
        const threshold = isOBTot ? 10 : 2;
        
        if (num < -threshold) {
            return `<span class="negative-diff">${rounded}</span>`;
        } else if (num > threshold) {
            return `<span class="positive-diff">${rounded}</span>`;
        }
        return `${rounded}`;
    }

    // Helper to calculate process totals with manual override support
    // Returns { weightedTarget, sumVarHrs, sumVol } for footer display
    function calcProcessTotals(data, processKey, getManualTarget) {
        let sumVolTimesTarget = 0;
        let sumVol = 0;
        let sumVarHrs = 0;
        let hasOverride = false;

        data.forEach(hourData => {
            const process = hourData[processKey] || {};
            const processVol = parseVol(process.vol || 0);
            const actHrs = normalizeNumber(process.hrs);
            const pprPlanRate = normalizeNumber(process.planRate);
            const manualRate = normalizeNumber(getManualTarget(hourData.timeRange));

            if (manualRate > 0) {
                hasOverride = true;
                // Use manual override
                sumVolTimesTarget += processVol * manualRate;
                sumVol += processVol;
                const targetHrs = processVol / manualRate;
                sumVarHrs += targetHrs - actHrs;
            } else {
                // Use PPR values
                sumVolTimesTarget += processVol * pprPlanRate;
                sumVol += processVol;
                sumVarHrs += normalizeNumber(process.varHrs);
            }
        });

        const weightedTarget = sumVol > 0 ? sumVolTimesTarget / sumVol : 0;
        return { weightedTarget, sumVarHrs, sumVol, hasOverride };
    }

    // Update footer totals for a process with override support
    function updateProcessFooterWithOverride(data, processKey, tphElementId, targetElementId, varElementId, getManualTarget, isOBTot = false) {
        const { weightedTarget, sumVarHrs, hasOverride } = calcProcessTotals(data, processKey, getManualTarget);
        
        if (hasOverride) {
            if (document.getElementById(targetElementId)) {
                document.getElementById(targetElementId).textContent = formatRate(weightedTarget);
            }
            if (document.getElementById(varElementId)) {
                document.getElementById(varElementId).innerHTML = formatVarHrs(sumVarHrs, isOBTot);
            }
        }
    }

    function renderProcessCell(process, vol, manualTargetValue, isOBTot = false, isWeekMode = false) {
        const actTPH = formatRate(process.rate);
        const pprPlanRate = normalizeNumber(process.planRate);
        const manualRate = normalizeNumber(manualTargetValue);
        
        // Manual override takes precedence over PPR target
        const useManualOverride = manualRate > 0;
        const planRateRaw = useManualOverride ? manualTargetValue : (pprPlanRate > 0 ? process.planRate : '0');
        const planRate = formatRate(planRateRaw);
        const actHrs = isWeekMode ? Math.round(normalizeNumber(process.hrs)) : formatHrsRound(process.hrs);
        
        // Recalculate variance hours if manual override is set
        let varHrs;
        if (useManualOverride) {
            // Use process's own volume for calculation
            const processVol = parseVol(process.vol || vol);
            const actHrsNum = normalizeNumber(process.hrs);
            const targetHrs = manualRate > 0 ? processVol / manualRate : 0;
            const varHrsNum = targetHrs - actHrsNum;
            varHrs = formatVarHrs(varHrsNum, isOBTot);
        } else {
            varHrs = formatVarHrs(process.varHrs, isOBTot);
        }

        return `<td>${actTPH}</td><td>${planRate}</td><td>${actHrs}</td><td>${varHrs}</td>`;
    }

    // Unified clickable process cell renderer (used by OB Direct and IB Direct tables)
    function renderClickableProcessCell(process, vol, manualTargetValue, processKey, timeParams, isWeekMode) {
        const actTPH = formatRate(process.rate);
        const pprPlanRate = normalizeNumber(process.planRate);
        const manualRate = normalizeNumber(manualTargetValue);
        
        // Manual override takes precedence over PPR target
        const useManualOverride = manualRate > 0;
        const planRateRaw = useManualOverride ? manualTargetValue : (pprPlanRate > 0 ? process.planRate : '0');
        const planRate = formatRate(planRateRaw);
        const actHrs = isWeekMode ? Math.round(normalizeNumber(process.hrs)) : formatHrsRound(process.hrs);
        
        // Recalculate variance hours if manual override is set
        let varHrs;
        if (useManualOverride) {
            // Use process's own volume for calculation
            const processVol = parseVol(process.vol || vol);
            const actHrsNum = normalizeNumber(process.hrs);
            const targetHrs = manualRate > 0 ? processVol / manualRate : 0;
            const varHrsNum = targetHrs - actHrsNum;
            varHrs = formatVarHrs(varHrsNum, false);
        } else {
            varHrs = formatVarHrs(process.varHrs, false);
        }
        
        const timeParamsJson = JSON.stringify(timeParams).replace(/"/g, '&quot;');
        const tphCell = `<td><span class="fr-tph-link" data-process="${processKey}" data-time-params="${timeParamsJson}">${actTPH}</span></td>`;
        return `${tphCell}<td>${planRate}</td><td>${actHrs}</td><td>${varHrs}</td>`;
    }

    function renderOBIndirectsTable(data) {
        const tbody = document.getElementById('obIndirectsTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No OB Indirects data to render');
            return;
        }

        const params = getFormParameters();
        const isWeekMode = params?.spanType === 'Week';
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];

        let html = '';
        const totals = { obVol: 0, obTot: 0, shipDock: 0, packSupport: 0, obLeadPA: 0, obProblemSolve: 0, pickSupport: 0 };

        data.forEach(hourData => {
            const obVol = hourData.obVol || '0';

            totals.obVol += parseVol(obVol);
            totals.obTot += normalizeNumber(hourData.obTot.hrs);
            totals.shipDock += normalizeNumber(hourData.shipDock.hrs);
            totals.packSupport += normalizeNumber(hourData.packSupport.hrs);
            totals.obLeadPA += normalizeNumber(hourData.obLeadPA.hrs);
            totals.obProblemSolve += normalizeNumber(hourData.obProblemSolve.hrs);
            totals.pickSupport += normalizeNumber(hourData.pickSupport.hrs);

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Breakdown modal
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            const obVolCell = `<td>${obVol}</td>`;

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                ${obVolCell}
                ${renderProcessCell(hourData.obTot, obVol, manualTarget.obTot, true, isWeekMode)}
                ${renderProcessCell(hourData.shipDock, obVol, manualTarget.shipDock, false, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.packSupport, obVol, manualTarget.packSupport, 'packSupport', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.obLeadPA, obVol, manualTarget.obLeadPA, 'obLeadPA', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.obProblemSolve, obVol, manualTarget.obProblemSolve, 'obProblemSolve', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.pickSupport, obVol, manualTarget.pickSupport, 'pickSupport', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for indirect function breakdown links
        tbody.querySelectorAll('.fr-indirect-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowIndirectFunctionBreakdown(processKey, timeParams, false);
            });
        });

        if (document.getElementById('totalOBVol')) document.getElementById('totalOBVol').textContent = Math.round(totals.obVol).toLocaleString('en-US');
        if (document.getElementById('totalOBTot')) document.getElementById('totalOBTot').textContent = Math.round(totals.obTot);
        if (document.getElementById('totalShipDock')) document.getElementById('totalShipDock').textContent = Math.round(totals.shipDock);
        if (document.getElementById('totalPackSupp')) document.getElementById('totalPackSupp').textContent = Math.round(totals.packSupport);
        if (document.getElementById('totalOBLead')) document.getElementById('totalOBLead').textContent = Math.round(totals.obLeadPA);
        if (document.getElementById('totalOBPS')) document.getElementById('totalOBPS').textContent = Math.round(totals.obProblemSolve);
        if (document.getElementById('totalPickSupp')) document.getElementById('totalPickSupp').textContent = Math.round(totals.pickSupport);

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        updateProcessFooterWithOverride(data, 'obTot', 'totalOBTotTPH', 'totalOBTotTarget', 'totalOBTotVar', tr => getTarget(tr, 'obTot'), true);
        updateProcessFooterWithOverride(data, 'shipDock', 'totalShipDockTPH', 'totalShipDockTarget', 'totalShipDockVar', tr => getTarget(tr, 'shipDock'));
        updateProcessFooterWithOverride(data, 'packSupport', 'totalPackSuppTPH', 'totalPackSuppTarget', 'totalPackSuppVar', tr => getTarget(tr, 'packSupport'));
        updateProcessFooterWithOverride(data, 'obLeadPA', 'totalOBLeadTPH', 'totalOBLeadTarget', 'totalOBLeadVar', tr => getTarget(tr, 'obLeadPA'));
        updateProcessFooterWithOverride(data, 'obProblemSolve', 'totalOBPSTPH', 'totalOBPSTarget', 'totalOBPSVar', tr => getTarget(tr, 'obProblemSolve'));
        updateProcessFooterWithOverride(data, 'pickSupport', 'totalPickSuppTPH', 'totalPickSuppTarget', 'totalPickSuppVar', tr => getTarget(tr, 'pickSupport'));

        // Make footer hours values clickable for total view
        const footerIndirectIds = {
            'totalPackSupp': 'packSupport',
            'totalOBLead': 'obLeadPA',
            'totalOBPS': 'obProblemSolve',
            'totalPickSupp': 'pickSupport'
        };

        for (const [elementId, processKey] of Object.entries(footerIndirectIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-indirect-link');
                element.style.cursor = 'pointer';
                element.style.textDecoration = 'underline';
                element.onclick = function() {
                    fetchAndShowIndirectFunctionBreakdown(processKey, null, true);
                };
            }
        }
    }

    function renderOBDirectTable(data) {
        const tbody = document.getElementById('obDirectTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No OB Direct data to render');
            return;
        }

        // Get date from form parameters for building Function Rollup URLs
        const params = getFormParameters();
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];
        const isWeekMode = params?.spanType === 'Week';

        let html = '';
        const totals = { obVol: 0, multiVol: 0, packMultisVol: 0, pick: 0, packSingle: 0, spp: 0, sort: 0, chuting: 0, packMultis: 0 };

        data.forEach(hourData => {
            const obVol = hourData.obVol || '0';
            const obVolNum = parseVol(obVol);

            // Multi Vol = Chuting vol + Pack Multis vol
            const chutingVol = parseVol(hourData.chuting.vol);
            const packMultisVol = parseVol(hourData.packMultis.vol);
            const multiVol = chutingVol + packMultisVol;
            const singleVol = obVolNum - multiVol;
            const multiPct = obVolNum > 0 ? (multiVol / obVolNum * 100) : 0;
            const p2rPct = multiVol > 0 ? (packMultisVol / multiVol * 100) : 0;

            totals.obVol += obVolNum;
            totals.multiVol += multiVol;
            totals.packMultisVol += packMultisVol;
            totals.pick += normalizeNumber(hourData.pick.hrs);
            totals.packSingle += normalizeNumber(hourData.packSingle.hrs);
            totals.spp += normalizeNumber(hourData.spp.hrs);
            totals.sort += normalizeNumber(hourData.sort.hrs);
            totals.chuting += normalizeNumber(hourData.chuting.hrs);
            totals.packMultis += normalizeNumber(hourData.packMultis.hrs);

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Rollup
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                // Week mode - use full day
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                // Intraday mode - parse time range
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                // Handle midnight crossing
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                <td>${Math.round(obVolNum).toLocaleString('en-US')}</td>
                <td>${Math.round(multiVol).toLocaleString('en-US')}</td>
                <td>${Math.round(singleVol).toLocaleString('en-US')}</td>
                <td>${multiPct.toFixed(1)}%</td>
                <td>${p2rPct.toFixed(1)}%</td>
                ${renderClickableProcessCell(hourData.pick, obVol, manualTarget.pick, 'pick', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.packSingle, obVol, manualTarget.packSingle, 'packSingle', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.spp, obVol, manualTarget.spp, 'spp', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.sort, obVol, manualTarget.sort, 'sort', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.chuting, obVol, manualTarget.chuting, 'chuting', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.packMultis, obVol, manualTarget.packMultis, 'packMultis', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for TPH links
        tbody.querySelectorAll('.fr-tph-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowFunctionRollupModal(processKey, timeParams, false);
            });
        });

        const totalSingleVol = totals.obVol - totals.multiVol;
        const totalMultiPct = totals.obVol > 0 ? (totals.multiVol / totals.obVol * 100) : 0;
        const totalP2RPct = totals.multiVol > 0 ? (totals.packMultisVol / totals.multiVol * 100) : 0;

        if (document.getElementById('totalOBVolDirect')) document.getElementById('totalOBVolDirect').textContent = Math.round(totals.obVol).toLocaleString('en-US');
        if (document.getElementById('totalMultiVol')) document.getElementById('totalMultiVol').textContent = Math.round(totals.multiVol).toLocaleString('en-US');
        if (document.getElementById('totalSingleVol')) document.getElementById('totalSingleVol').textContent = Math.round(totalSingleVol).toLocaleString('en-US');
        if (document.getElementById('totalMultiPct')) document.getElementById('totalMultiPct').textContent = totalMultiPct.toFixed(1) + '%';
        if (document.getElementById('totalP2RPct')) document.getElementById('totalP2RPct').textContent = totalP2RPct.toFixed(1) + '%';
        if (document.getElementById('totalPick')) document.getElementById('totalPick').textContent = Math.round(totals.pick);
        if (document.getElementById('totalPackSi')) document.getElementById('totalPackSi').textContent = Math.round(totals.packSingle);
        if (document.getElementById('totalSPP')) document.getElementById('totalSPP').textContent = Math.round(totals.spp);
        if (document.getElementById('totalSort')) document.getElementById('totalSort').textContent = Math.round(totals.sort);
        if (document.getElementById('totalChuting')) document.getElementById('totalChuting').textContent = Math.round(totals.chuting);
        if (document.getElementById('totalPackMu')) document.getElementById('totalPackMu').textContent = Math.round(totals.packMultis);

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        updateProcessFooterWithOverride(data, 'pick', 'totalPickTPH', 'totalPickTarget', 'totalPickVar', tr => getTarget(tr, 'pick'));
        updateProcessFooterWithOverride(data, 'packSingle', 'totalPackSiTPH', 'totalPackSiTarget', 'totalPackSiVar', tr => getTarget(tr, 'packSingle'));
        updateProcessFooterWithOverride(data, 'spp', 'totalSPPTPH', 'totalSPPTarget', 'totalSPPVar', tr => getTarget(tr, 'spp'));
        updateProcessFooterWithOverride(data, 'sort', 'totalSortTPH', 'totalSortTarget', 'totalSortVar', tr => getTarget(tr, 'sort'));
        updateProcessFooterWithOverride(data, 'chuting', 'totalChutingTPH', 'totalChutingTarget', 'totalChutingVar', tr => getTarget(tr, 'chuting'));
        updateProcessFooterWithOverride(data, 'packMultis', 'totalPackMuTPH', 'totalPackMuTarget', 'totalPackMuVar', tr => getTarget(tr, 'packMultis'));

        // Make footer TPH values clickable for total view
        const footerTPHIds = {
            'totalPickTPH': 'pick',
            'totalPackSiTPH': 'packSingle',
            'totalSPPTPH': 'spp',
            'totalSortTPH': 'sort',
            'totalChutingTPH': 'chuting',
            'totalPackMuTPH': 'packMultis'
        };

        for (const [elementId, processKey] of Object.entries(footerTPHIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-tph-link');
                element.style.cursor = 'pointer';
                element.onclick = function() {
                    fetchAndShowFunctionRollupModal(processKey, null, true);
                };
            }
        }
    }

    function renderIBIndirectsTable(data) {
        const tbody = document.getElementById('ibIndirectsTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No IB Indirects data to render');
            return;
        }

        const params = getFormParameters();
        const isWeekMode = params?.spanType === 'Week';
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];

        let html = '';
        const totals = { ibVol: 0, ibTot: 0, receiveDock: 0, receiveSupport: 0, tsiSupport: 0, stpSupport: 0, rsrSupport: 0, ibLeadPA: 0 };

        data.forEach(hourData => {
            const ibVol = hourData.ibTotal?.vol || hourData.ibTot?.vol || '0';

            totals.ibVol += parseVol(ibVol);
            totals.ibTot += normalizeNumber(hourData.ibTotal?.hrs || hourData.ibTot?.hrs);
            totals.receiveDock += normalizeNumber(hourData.receiveDock?.hrs);
            totals.receiveSupport += normalizeNumber(hourData.receiveSupport?.hrs);
            totals.tsiSupport += normalizeNumber(hourData.tsiSupport?.hrs);
            totals.stpSupport += normalizeNumber(hourData.stpSupport?.hrs);
            totals.rsrSupport += normalizeNumber(hourData.rsrSupport?.hrs);
            totals.ibLeadPA += normalizeNumber(hourData.ibLeadPA?.hrs);

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Breakdown modal
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            const ibVolCell = `<td>${ibVol}</td>`;

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                ${ibVolCell}
                ${renderProcessCell(hourData.ibTotal || hourData.ibTot || {}, ibVol, manualTarget.ibTot, true, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.receiveDock || {}, ibVol, manualTarget.receiveDock, 'receiveDock', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.receiveSupport || {}, ibVol, manualTarget.receiveSupport, 'receiveSupport', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.tsiSupport || {}, ibVol, manualTarget.tsiSupport, 'tsiSupport', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.stpSupport || {}, ibVol, manualTarget.stpSupport, 'stpSupport', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.rsrSupport || {}, ibVol, manualTarget.rsrSupport, 'rsrSupport', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.ibLeadPA || {}, ibVol, manualTarget.ibLeadPA, 'ibLead', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for indirect function breakdown links
        tbody.querySelectorAll('.fr-indirect-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowIndirectFunctionBreakdown(processKey, timeParams, false);
            });
        });

        // Update footer totals
        const setTotal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setTotal('totalIBVolInd', Math.round(totals.ibVol).toLocaleString('en-US'));
        setTotal('totalIBTotHrs', Math.round(totals.ibTot));
        setTotal('totalRecDock', Math.round(totals.receiveDock));
        setTotal('totalRecSupp', Math.round(totals.receiveSupport));
        setTotal('totalTSISupp', Math.round(totals.tsiSupport));
        setTotal('totalStPSupp', Math.round(totals.stpSupport));
        setTotal('totalRSRSupp', Math.round(totals.rsrSupport));
        setTotal('totalIBLead', Math.round(totals.ibLeadPA));

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        // For ibTot, need to handle ibTotal/ibTot key difference
        const ibTotData = data.map(h => ({ ...h, ibTot: h.ibTotal || h.ibTot || {} }));
        updateProcessFooterWithOverride(ibTotData, 'ibTot', 'totalIBTotTPH', 'totalIBTotTarget', 'totalIBTotVar', tr => getTarget(tr, 'ibTot'), true);
        updateProcessFooterWithOverride(data, 'receiveDock', 'totalRecDockTPH', 'totalRecDockTarget', 'totalRecDockVar', tr => getTarget(tr, 'receiveDock'));
        updateProcessFooterWithOverride(data, 'receiveSupport', 'totalRecSuppTPH', 'totalRecSuppTarget', 'totalRecSuppVar', tr => getTarget(tr, 'receiveSupport'));
        updateProcessFooterWithOverride(data, 'tsiSupport', 'totalTSISuppTPH', 'totalTSISuppTarget', 'totalTSISuppVar', tr => getTarget(tr, 'tsiSupport'));
        updateProcessFooterWithOverride(data, 'stpSupport', 'totalStPSuppTPH', 'totalStPSuppTarget', 'totalStPSuppVar', tr => getTarget(tr, 'stpSupport'));
        updateProcessFooterWithOverride(data, 'rsrSupport', 'totalRSRSuppTPH', 'totalRSRSuppTarget', 'totalRSRSuppVar', tr => getTarget(tr, 'rsrSupport'));
        updateProcessFooterWithOverride(data, 'ibLeadPA', 'totalIBLeadTPH', 'totalIBLeadTarget', 'totalIBLeadVar', tr => getTarget(tr, 'ibLeadPA'));

        // Make footer hours values clickable for total view
        const footerIndirectIds = {
            'totalRecDock': 'receiveDock',
            'totalRecSupp': 'receiveSupport',
            'totalTSISupp': 'tsiSupport',
            'totalStPSupp': 'stpSupport',
            'totalRSRSupp': 'rsrSupport',
            'totalIBLead': 'ibLead'
        };

        for (const [elementId, processKey] of Object.entries(footerIndirectIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-indirect-link');
                element.style.cursor = 'pointer';
                element.style.textDecoration = 'underline';
                element.onclick = function() {
                    fetchAndShowIndirectFunctionBreakdown(processKey, null, true);
                };
            }
        }
    }

    function renderIBDirectTable(data) {
        const tbody = document.getElementById('ibDirectTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No IB Direct data to render');
            return;
        }

        // Get date from form parameters for building Function Rollup URLs
        const params = getFormParameters();
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];
        const isWeekMode = params?.spanType === 'Week';

        let html = '';
        const totals = { ibVol: 0, tsiVol: 0, nvfVol: 0, eachTSI: 0, tsiDecant: 0, eachStP: 0, prepRecorder: 0, cubiscan: 0, stowHC: 0, stowHCCount: 0 };

        data.forEach(hourData => {
            const ibVol = hourData.ibTotal?.vol || hourData.ibTot?.vol || '0';
            const ibVolNum = parseVol(ibVol);

            const tsiVolNum = parseVol(hourData.tsiVol?.vol || 0);
            const nvfVolNum = parseVol(hourData.nvfVol?.vol || 0);
            const tsiPct = ibVolNum > 0 ? (tsiVolNum / ibVolNum * 100) : 0;

            // Calculate Stow HC = Each TSI Hours + Each StP Hours
            const eachTSIHrs = normalizeNumber(hourData.eachTSI?.hrs);
            const eachStPHrs = normalizeNumber(hourData.eachStP?.hrs);
            const stowHC = eachTSIHrs + eachStPHrs;

            totals.ibVol += ibVolNum;
            totals.tsiVol += tsiVolNum;
            totals.nvfVol += nvfVolNum;
            totals.eachTSI += eachTSIHrs;
            totals.tsiDecant += normalizeNumber(hourData.tsiDecant?.hrs);
            totals.eachStP += eachStPHrs;
            totals.prepRecorder += normalizeNumber(hourData.prepRecorder?.hrs);
            totals.cubiscan += normalizeNumber(hourData.cubiscan?.hrs);
            totals.stowHC += stowHC;
            totals.stowHCCount++;

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Rollup
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                // Week mode - use full day
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                // Intraday mode - parse time range
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                // Handle midnight crossing
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                <td>${Math.round(ibVolNum).toLocaleString('en-US')}</td>
                <td>${Math.round(tsiVolNum).toLocaleString('en-US')}</td>
                <td>${Math.round(nvfVolNum).toLocaleString('en-US')}</td>
                <td>${tsiPct.toFixed(1)}%</td>
                <td>${Math.round(stowHC)}</td>
                ${renderClickableProcessCell(hourData.eachTSI || {}, ibVol, manualTarget.eachTSI, 'eachTsi', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.tsiDecant || {}, ibVol, manualTarget.tsiDecant, 'tsiDecant', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.eachStP || {}, ibVol, manualTarget.eachStP, 'eachStp', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.prepRecorder || {}, ibVol, manualTarget.prepRecorder, 'prepReceiver', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.cubiscan || {}, ibVol, manualTarget.cubiscan, 'cubiscan', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for TPH links
        tbody.querySelectorAll('.fr-tph-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowFunctionRollupModal(processKey, timeParams, false);
            });
        });

        const totalTsiPct = totals.ibVol > 0 ? (totals.tsiVol / totals.ibVol * 100) : 0;
        const avgStowHC = totals.stowHCCount > 0 ? totals.stowHC / totals.stowHCCount : 0;

        // Update footer totals
        const setTotal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.textContent = val;
        };

        setTotal('totalIBVolDir', Math.round(totals.ibVol).toLocaleString('en-US'));
        setTotal('totalTSIVol', Math.round(totals.tsiVol).toLocaleString('en-US'));
        setTotal('totalNVFVol', Math.round(totals.nvfVol).toLocaleString('en-US'));
        setTotal('totalTSIPct', totalTsiPct.toFixed(1) + '%');
        setTotal('totalStowHC', Math.round(avgStowHC));
        setTotal('totalEachTSI', Math.round(totals.eachTSI));
        setTotal('totalTSIDecant', Math.round(totals.tsiDecant));
        setTotal('totalEachStP', Math.round(totals.eachStP));
        setTotal('totalPrepRec', Math.round(totals.prepRecorder));
        setTotal('totalCubiscan', Math.round(totals.cubiscan));

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        updateProcessFooterWithOverride(data, 'eachTSI', 'totalEachTSITPH', 'totalEachTSITarget', 'totalEachTSIVar', tr => getTarget(tr, 'eachTSI'));
        updateProcessFooterWithOverride(data, 'tsiDecant', 'totalTSIDecantTPH', 'totalTSIDecantTarget', 'totalTSIDecantVar', tr => getTarget(tr, 'tsiDecant'));
        updateProcessFooterWithOverride(data, 'eachStP', 'totalEachStPTPH', 'totalEachStPTarget', 'totalEachStPVar', tr => getTarget(tr, 'eachStP'));
        updateProcessFooterWithOverride(data, 'prepRecorder', 'totalPrepRecTPH', 'totalPrepRecTarget', 'totalPrepRecVar', tr => getTarget(tr, 'prepRecorder'));
        updateProcessFooterWithOverride(data, 'cubiscan', 'totalCubiscanTPH', 'totalCubiscanTarget', 'totalCubiscanVar', tr => getTarget(tr, 'cubiscan'));

        // Make footer TPH values clickable for total view
        const footerTPHIds = {
            'totalEachTSITPH': 'eachTsi',
            'totalTSIDecantTPH': 'tsiDecant',
            'totalEachStPTPH': 'eachStp',
            'totalPrepRecTPH': 'prepReceiver',
            'totalCubiscanTPH': 'cubiscan'
        };

        for (const [elementId, processKey] of Object.entries(footerTPHIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-tph-link');
                element.style.cursor = 'pointer';
                element.onclick = function() {
                    fetchAndShowFunctionRollupModal(processKey, null, true);
                };
            }
        }
    }

    function renderTransferOutTable(data) {
        const tbody = document.getElementById('transferOutTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No Transfer Out data to render');
            return;
        }

        const params = getFormParameters();
        const isWeekMode = params?.spanType === 'Week';
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];

        let html = '';
        const totals = { daVol: 0, daBldgTransfer: 0, transferOutPick: 0, transferOut: 0, transferOutDock: 0, toLeadPA: 0 };

        data.forEach(hourData => {
            const daVol = hourData.daBldgTransferVol || '0';

            totals.daVol += parseVol(daVol);
            totals.daBldgTransfer += normalizeNumber(hourData.daBldgTransfer.hrs);
            totals.transferOutPick += normalizeNumber(hourData.transferOutPick?.hrs);
            totals.transferOut += normalizeNumber(hourData.transferOut.hrs);
            totals.transferOutDock += normalizeNumber(hourData.transferOutDock.hrs);
            totals.toLeadPA += normalizeNumber(hourData.toLeadPA.hrs);

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Breakdown modal
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            const daVolCell = `<td>${daVol}</td>`;

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                ${daVolCell}
                ${renderProcessCell(hourData.daBldgTransfer, daVol, manualTarget.daBldgTransfer, false, isWeekMode)}
                ${renderClickableProcessCell(hourData.transferOutPick || {}, daVol, manualTarget.transferOutPick, 'transferOutPick', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.transferOut, daVol, manualTarget.transferOut, 'transferOut', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.transferOutDock, daVol, manualTarget.transferOutDock, 'transferOutDock', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.toLeadPA, daVol, manualTarget.toLeadPA, 'toLeadPA', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for direct process TPH links
        tbody.querySelectorAll('.fr-tph-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowFunctionRollupModal(processKey, timeParams, false);
            });
        });

        // Add click handlers for indirect function breakdown links
        tbody.querySelectorAll('.fr-indirect-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowIndirectFunctionBreakdown(processKey, timeParams, false);
            });
        });

        if (document.getElementById('totalDAVol')) document.getElementById('totalDAVol').textContent = Math.round(totals.daVol).toLocaleString('en-US');
        if (document.getElementById('totalDABldg')) document.getElementById('totalDABldg').textContent = Math.round(totals.daBldgTransfer);
        if (document.getElementById('totalTOPick')) document.getElementById('totalTOPick').textContent = Math.round(totals.transferOutPick);
        if (document.getElementById('totalTransOut')) document.getElementById('totalTransOut').textContent = Math.round(totals.transferOut);
        if (document.getElementById('totalTransDock')) document.getElementById('totalTransDock').textContent = Math.round(totals.transferOutDock);
        if (document.getElementById('totalTOLead')) document.getElementById('totalTOLead').textContent = Math.round(totals.toLeadPA);

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        updateProcessFooterWithOverride(data, 'daBldgTransfer', 'totalDABldgTPH', 'totalDABldgTarget', 'totalDABldgVar', tr => getTarget(tr, 'daBldgTransfer'));
        updateProcessFooterWithOverride(data, 'transferOutPick', 'totalTOPickTPH', 'totalTOPickTarget', 'totalTOPickVar', tr => getTarget(tr, 'transferOutPick'));
        updateProcessFooterWithOverride(data, 'transferOut', 'totalTransOutTPH', 'totalTransOutTarget', 'totalTransOutVar', tr => getTarget(tr, 'transferOut'));
        updateProcessFooterWithOverride(data, 'transferOutDock', 'totalTransDockTPH', 'totalTransDockTarget', 'totalTransDockVar', tr => getTarget(tr, 'transferOutDock'));
        updateProcessFooterWithOverride(data, 'toLeadPA', 'totalTOLeadTPH', 'totalTOLeadTarget', 'totalTOLeadVar', tr => getTarget(tr, 'toLeadPA'));

        // Make footer hours values clickable for total view
        const footerIndirectIds = {
            'totalTransOut': 'transferOut',
            'totalTransDock': 'transferOutDock',
            'totalTOLead': 'toLeadPA'
        };

        for (const [elementId, processKey] of Object.entries(footerIndirectIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-indirect-link');
                element.style.cursor = 'pointer';
                element.style.textDecoration = 'underline';
                element.onclick = function() {
                    fetchAndShowIndirectFunctionBreakdown(processKey, null, true);
                };
            }
        }

        // Make footer TPH values clickable for direct processes
        const footerDirectTPHIds = {
            'totalTOPickTPH': 'transferOutPick'
        };

        for (const [elementId, processKey] of Object.entries(footerDirectTPHIds)) {
            const element = document.getElementById(elementId);
            if (element) {
                element.classList.add('fr-tph-link');
                element.style.cursor = 'pointer';
                element.onclick = function() {
                    fetchAndShowFunctionRollupModal(processKey, null, true);
                };
            }
        }
    }

    function renderVendorReturnsTable(data) {
        const tbody = document.getElementById('vendorReturnsTableBody');
        if (!tbody) return;
        if (!data || data.length === 0) {
            console.warn('⚠️ No Vendor Returns data to render');
            return;
        }

        const params = getFormParameters();
        const isWeekMode = params?.spanType === 'Week';
        const formStartDate = params?.startDateStr || document.getElementById('startDateDay')?.value || document.getElementById('startDateIntraday')?.value || new Date().toISOString().split('T')[0];

        let html = '';
        const totals = { vretVol: 0, vretTot: 0, vretPick: 0, vretPack: 0, vretSupport: 0 };

        data.forEach(hourData => {
            const vretVol = parseVol(hourData.vretVol);

            totals.vretVol += vretVol;
            totals.vretTot += normalizeNumber(hourData.vretTot.hrs);
            totals.vretPick += normalizeNumber(hourData.vretPick.hrs);
            totals.vretPack += normalizeNumber(hourData.vretPack.hrs);
            totals.vretSupport += normalizeNumber(hourData.vretSupport.hrs);

            const targetKey = hourData.timeRange;
            const manualTarget = hourlyProcessPathTargets[targetKey] || {};

            // Build time params for Function Breakdown modal
            let timeParams;
            const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            
            if (isWeekMode || dayNames.includes(hourData.timeRange)) {
                timeParams = {
                    startDateStr: hourData.dateStr,
                    isFullDay: true
                };
            } else {
                const [startTime, endTime] = hourData.timeRange.split('-');
                const startHour = parseInt(startTime.split(':')[0]);
                const startMinute = parseInt(startTime.split(':')[1]) || 0;
                let endHour = parseInt(endTime.split(':')[0]);
                const endMinute = parseInt(endTime.split(':')[1]) || 0;
                
                const baseDate = hourData.dateStr || formStartDate;
                let endDateStr = baseDate;
                
                if (endHour === 24 || (endHour === 0 && startHour > 0)) {
                    endHour = endHour === 24 ? 0 : endHour;
                    const nextDay = new Date(baseDate);
                    nextDay.setDate(nextDay.getDate() + 1);
                    endDateStr = nextDay.toISOString().split('T')[0];
                }

                timeParams = {
                    startDateStr: baseDate,
                    startHour: startHour,
                    startMinute: startMinute,
                    endDateStr: endDateStr,
                    endHour: endHour,
                    endMinute: endMinute
                };
            }

            html += `<tr>
                <td>${buildPPRTimeLink(hourData.timeRange, hourData.dateStr)}</td>
                <td>${Math.round(vretVol).toLocaleString('en-US')}</td>
                ${renderProcessCell(hourData.vretTot, vretVol, manualTarget.vretTot, false, isWeekMode)}
                ${renderClickableProcessCell(hourData.vretPick, vretVol, manualTarget.vretPick, 'vretPick', timeParams, isWeekMode)}
                ${renderClickableProcessCell(hourData.vretPack, vretVol, manualTarget.vretPack, 'vretPack', timeParams, isWeekMode)}
                ${renderClickableIndirectProcessCell(hourData.vretSupport, vretVol, manualTarget.vretSupport, 'vretSupport', timeParams, isWeekMode)}
            </tr>`;
        });

        tbody.innerHTML = html;

        // Add click handlers for direct process TPH links
        tbody.querySelectorAll('.fr-tph-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowFunctionRollupModal(processKey, timeParams, false);
            });
        });

        // Add click handlers for indirect function breakdown links
        tbody.querySelectorAll('.fr-indirect-link').forEach(link => {
            link.addEventListener('click', function() {
                const processKey = this.dataset.process;
                const timeParams = JSON.parse(this.dataset.timeParams.replace(/&quot;/g, '"'));
                fetchAndShowIndirectFunctionBreakdown(processKey, timeParams, false);
            });
        });

        if (document.getElementById('totalVRETVol')) document.getElementById('totalVRETVol').textContent = Math.round(totals.vretVol).toLocaleString('en-US');
        if (document.getElementById('totalVRETTot')) document.getElementById('totalVRETTot').textContent = Math.round(totals.vretTot);
        if (document.getElementById('totalVRETPick')) document.getElementById('totalVRETPick').textContent = Math.round(totals.vretPick);
        if (document.getElementById('totalVRETPack')) document.getElementById('totalVRETPack').textContent = Math.round(totals.vretPack);
        if (document.getElementById('totalVRETSupp')) document.getElementById('totalVRETSupp').textContent = Math.round(totals.vretSupport);

        // Update footer totals with override support
        const getTarget = (timeRange, field) => (hourlyProcessPathTargets[timeRange] || {})[field];
        updateProcessFooterWithOverride(data, 'vretTot', 'totalVRETTotTPH', 'totalVRETTotTarget', 'totalVRETTotVar', tr => getTarget(tr, 'vretTot'));
        updateProcessFooterWithOverride(data, 'vretPick', 'totalVRETPickTPH', 'totalVRETPickTarget', 'totalVRETPickVar', tr => getTarget(tr, 'vretPick'));
        updateProcessFooterWithOverride(data, 'vretPack', 'totalVRETPackTPH', 'totalVRETPackTarget', 'totalVRETPackVar', tr => getTarget(tr, 'vretPack'));
        updateProcessFooterWithOverride(data, 'vretSupport', 'totalVRETSuppTPH', 'totalVRETSuppTarget', 'totalVRETSuppVar', tr => getTarget(tr, 'vretSupport'));

        // Make footer hours value clickable for total view
        const element = document.getElementById('totalVRETSupp');
        if (element) {
            element.classList.add('fr-indirect-link');
            element.style.cursor = 'pointer';
            element.style.textDecoration = 'underline';
            element.onclick = function() {
                fetchAndShowIndirectFunctionBreakdown('vretSupport', null, true);
            };
        }

        // Make footer TPH values clickable for direct processes
        const footerDirectTPHIds = {
            'totalVRETPickTPH': 'vretPick',
            'totalVRETPackTPH': 'vretPack'
        };

        for (const [elementId, processKey] of Object.entries(footerDirectTPHIds)) {
            const element2 = document.getElementById(elementId);
            if (element2) {
                element2.classList.add('fr-tph-link');
                element2.style.cursor = 'pointer';
                element2.onclick = function() {
                    fetchAndShowFunctionRollupModal(processKey, null, true);
                };
            }
        }
    }

    function renderTargetSettings() {
        renderIndirectTargetsTable();
        renderProcessPathTargetsTable();
    }

    // Process Path TPH Target Categories
    const processPathTargetCategories = [
        {
            id: 'obIndirects',
            name: 'OB Indirects',
            processes: [
                { id: 'obTot', name: 'OB Total' },
                { id: 'shipDock', name: 'Ship Dock' },
                { id: 'packSupport', name: 'Pack Support' },
                { id: 'obLeadPA', name: 'OB Lead/PA' },
                { id: 'obProblemSolve', name: 'OB Problem Solve' },
                { id: 'pickSupport', name: 'Pick Support' }
            ]
        },
        {
            id: 'obDirect',
            name: 'OB Direct',
            processes: [
                { id: 'pick', name: 'Pick' },
                { id: 'packSingle', name: 'Pack Single' },
                { id: 'spp', name: 'SPP' },
                { id: 'sort', name: 'Sort' },
                { id: 'chuting', name: 'Chuting' },
                { id: 'packMultis', name: 'Pack Multis' }
            ]
        },
        {
            id: 'transferOut',
            name: 'Transfer Out',
            processes: [
                { id: 'daBldgTransfer', name: 'DA Bldg Transfer' },
                { id: 'transferOutPick', name: 'Transfer Out Pick' },
                { id: 'transferOut', name: 'Transfer Out' },
                { id: 'transferOutDock', name: 'Transfer Out Dock' },
                { id: 'toLeadPA', name: 'TO Lead/PA' }
            ]
        },
        {
            id: 'vendorReturns',
            name: 'Vendor Returns',
            processes: [
                { id: 'vretTot', name: 'VRET Total' },
                { id: 'vretPick', name: 'VRET Pick' },
                { id: 'vretPack', name: 'VRET Pack' },
                { id: 'vretSupport', name: 'VRET Support' }
            ]
        },
        {
            id: 'ibIndirects',
            name: 'IB Indirects',
            processes: [
                { id: 'ibTot', name: 'IB Total' },
                { id: 'receiveDock', name: 'Receive Dock' },
                { id: 'receiveSupport', name: 'Receive Support' },
                { id: 'tsiSupport', name: 'TSI Support' },
                { id: 'stpSupport', name: 'StP Support' },
                { id: 'rsrSupport', name: 'RSR Support' },
                { id: 'ibLeadPA', name: 'IB Lead/PA' }
            ]
        },
        {
            id: 'ibDirect',
            name: 'IB Direct',
            processes: [
                { id: 'eachTSI', name: 'Each TSI' },
                { id: 'tsiDecant', name: 'TSI Decant' },
                { id: 'eachStP', name: 'Each StP' },
                { id: 'prepRecorder', name: 'Prep Recorder' },
                { id: 'cubiscan', name: 'Cubiscan' }
            ]
        }
    ];

    function renderProcessPathTargetsTable() {
        const container = document.getElementById('processPathTargetsContainer');
        if (!container) return;

        // Get all unique process IDs for headers
        const allProcesses = processPathTargetCategories.flatMap(cat => cat.processes);

        let html = '';

        // Create a table for each category
        processPathTargetCategories.forEach(category => {
            html += `
            <div class="process-path-target-section" style="margin-bottom: 20px;">
                <div style="font-weight: 600; margin-bottom: 8px; color: #232f3e;">${category.name}</div>
                <div class="hourly-container" style="max-height: 400px;">
                    <table class="hourly-table">
                        <thead>
                            <tr>
                                <th>Time</th>
                                ${category.processes.map(p => `<th>${p.name}</th>`).join('')}
                            </tr>
                        </thead>
                        <tbody>`;

            // Add "Set All" row
            html += `<tr style="background: #e8f4fc; font-weight: 600;"><td>Set All ↓</td>`;
            category.processes.forEach(process => {
                html += `<td><input type="text" class="target-settings-input pp-set-all-input" data-category="${category.id}" data-field="${process.id}" placeholder="..." style="background: #fff3cd; width: 60px;"></td>`;
            });
            html += '</tr>';

            // Add hourly rows
            for (let hour = 0; hour < 24; hour++) {
                const nextHour = (hour + 1) % 24;
                const timeRange = `${String(hour).padStart(2, '0')}:00-${nextHour === 0 ? '24' : String(nextHour).padStart(2, '0')}:00`;
                const savedTarget = hourlyProcessPathTargets[timeRange] || {};

                html += `<tr><td>${timeRange}</td>`;
                category.processes.forEach(process => {
                    const targetValue = savedTarget[process.id] !== undefined && savedTarget[process.id] !== 0 ? savedTarget[process.id] : '';
                    html += `<td><input type="text" class="target-settings-input pp-hourly-input" data-time="${timeRange}" data-field="${process.id}" value="${targetValue}" style="width: 60px;"></td>`;
                });
                html += '</tr>';
            }

            html += `</tbody></table></div></div>`;
        });

        container.innerHTML = html;

        // Handle "Set All" inputs
        container.querySelectorAll('.pp-set-all-input').forEach(input => {
            input.addEventListener('change', function() {
                const field = this.dataset.field;
                const value = this.value.trim() === '' ? 0 : (parseFloat(this.value) || 0);

                // Set value for all hourly inputs in this column
                container.querySelectorAll(`.pp-hourly-input[data-field="${field}"]`).forEach(hourlyInput => {
                    hourlyInput.value = value === 0 ? '' : value;
                    const time = hourlyInput.dataset.time;

                    if (!hourlyProcessPathTargets[time]) {
                        hourlyProcessPathTargets[time] = {};
                    }
                    hourlyProcessPathTargets[time][field] = value;
                });

                autoSaveTargets();
                this.value = '';
                this.placeholder = '✓';
                setTimeout(() => { this.placeholder = '...'; }, 1000);
            });
        });

        // Handle individual hourly inputs
        container.querySelectorAll('.pp-hourly-input').forEach(input => {
            input.addEventListener('change', function() {
                const time = this.dataset.time;
                const field = this.dataset.field;
                const value = this.value.trim() === '' ? 0 : (parseFloat(this.value) || 0);

                if (!hourlyProcessPathTargets[time]) {
                    hourlyProcessPathTargets[time] = {};
                }
                hourlyProcessPathTargets[time][field] = value;

                autoSaveTargets();
            });
        });
    }

    function renderIndirectTargetsTable() {
        const tbody = document.getElementById('targetIndirectTableBody');
        const thead = document.getElementById('targetIndirectTableHead');
        if (!tbody) return;

        // Get bucket configuration for dynamic rendering
        const bucketConfig = getBucketConfig();
        const buckets = bucketConfig.buckets;

        // Update table headers dynamically
        if (thead) {
            let headerHtml = '<tr><th>Time</th>';
            buckets.forEach(bucket => {
                headerHtml += `<th>${bucket.name}</th>`;
            });
            headerHtml += '</tr>';
            thead.innerHTML = headerHtml;
        }

        let html = '';

        // Add "Set All" row at the top
        html += '<tr style="background: #e8f4fc; font-weight: 600;"><td>Set All ↓</td>';
        buckets.forEach(bucket => {
            html += `<td><input type="text" class="target-settings-input set-all-input" data-field="${bucket.id}" placeholder="..." style="background: #fff3cd;"></td>`;
        });
        html += '</tr>';

        for (let hour = 0; hour < 24; hour++) {
            const nextHour = (hour + 1) % 24;
            const timeRange = `${String(hour).padStart(2, '0')}:00-${nextHour === 0 ? '24' : String(nextHour).padStart(2, '0')}:00`;

            const savedTarget = hourlyIndirectTargets[timeRange] || {};

            html += `<tr><td>${timeRange}</td>`;
            
            buckets.forEach(bucket => {
                const targetValue = savedTarget[bucket.id] !== undefined ? savedTarget[bucket.id] : 0;
                html += `<td><input type="text" class="target-settings-input hourly-input" data-time="${timeRange}" data-field="${bucket.id}" value="${targetValue}"></td>`;
            });
            
            html += '</tr>';
        }

        tbody.innerHTML = html;

        // Handle "Set All" inputs
        tbody.querySelectorAll('.set-all-input').forEach(input => {
            input.addEventListener('change', function() {
                const field = this.dataset.field;
                const value = parseFloat(this.value) || 0;
                
                // Set value for all hourly inputs in this column
                tbody.querySelectorAll(`.hourly-input[data-field="${field}"]`).forEach(hourlyInput => {
                    hourlyInput.value = value;
                    const time = hourlyInput.dataset.time;
                    
                    if (!hourlyIndirectTargets[time]) {
                        hourlyIndirectTargets[time] = {};
                    }
                    hourlyIndirectTargets[time][field] = value;
                });
                
                autoSaveTargets();
                this.value = ''; // Clear the "Set All" input after applying
                this.placeholder = '✓';
                setTimeout(() => { this.placeholder = '...'; }, 1000);
            });
        });

        // Handle individual hourly inputs
        tbody.querySelectorAll('.hourly-input').forEach(input => {
            input.addEventListener('change', function() {
                const time = this.dataset.time;
                const field = this.dataset.field;
                const value = parseFloat(this.value) || 0;

                if (!hourlyIndirectTargets[time]) {
                    hourlyIndirectTargets[time] = {};
                }
                hourlyIndirectTargets[time][field] = value;

                autoSaveTargets();
            });
        });
    }

    let isFetching = false;

    async function fetchAllData() {
        const fc = getWarehouseId();
        if (!fc) {
            alert('⚠️ Please enter FC code first!');
            return;
        }

        // Prevent multiple simultaneous fetches
        if (isFetching) {
            return;
        }
        isFetching = true;

        try {
        failedSlots = [];

        // Calculate dynamic totalFetchSteps based on date selection
        const params = getFormParameters();
        let slotsToFetch = 24; // Default: all 24 hours
        
        if (params) {
            if (params.spanType === 'Week') {
                // For Week mode, fetch daily data (7 days max)
                slotsToFetch = params.daysToFetch || 7;
            } else if (params.spanType === 'Intraday') {
                // Use generateTimeSlots directly so future-hour filtering is applied
                slotsToFetch = generateTimeSlots(params, params.startDateStr).length;
            } else if (params.startDateStr) {
                const selectedDate = new Date(params.startDateStr);
                const today = new Date();
                today.setHours(0, 0, 0, 0);
                selectedDate.setHours(0, 0, 0, 0);
                
                if (selectedDate.getTime() === today.getTime()) {
                    // For today, only fetch hours 0 to current hour
                    slotsToFetch = new Date().getHours() + 1;
                } else if (selectedDate.getTime() > today.getTime()) {
                    // Future date - minimal steps
                    slotsToFetch = 0;
                }
            }
        }
        
        // Calculate total steps for loading bar (PPR slots + Support slots * 3 + summary + time off task + full day PPR)
        totalFetchSteps = 1 + slotsToFetch + (slotsToFetch * 3) + 3 + 1;
        currentFetchStep = 0;
        updateLoadingBar();

        // Start ALL fetches in parallel for maximum speed
        const processPathPromise = fetchHourlyProcessPathData();
        const fullDayPPRPromise = fetchFullDayProcessPathTotals();
        const hourlyPromise = fetchHourlyData();
        const summaryPromise = fetchSummaryData();
        const timeOffTaskPromise = fetchTimeOnTaskData();

        // Wait for ALL data to complete before rendering anything
        // This ensures the page renders completely at once instead of piece by piece
        const [processPath, fullDayPPR, hourly, summary, timeOffTask] = await Promise.all([
            processPathPromise.catch(e => { console.error('❌ Process path fetch failed:', e); return null; }),
            fullDayPPRPromise.catch(e => { console.error('❌ Full day PPR fetch failed:', e); return null; }),
            hourlyPromise.catch(e => { console.error('❌ Hourly fetch failed:', e); return null; }),
            summaryPromise.catch(e => { console.error('❌ Summary fetch failed:', e); return null; }),
            timeOffTaskPromise.catch(e => { console.error('❌ Time off task fetch failed:', e); return null; })
        ]);

        // Use requestAnimationFrame to batch all DOM updates together
        // This prevents multiple reflows and allows the browser to paint efficiently
        // Stagger table renders to prevent browser lag
        // Each render gets its own animation frame for smoother painting
        // Loading bar stays visible until all renders complete
        const renderQueue = [];
        
        // 1. Department Overview & Process Path tables
        if (processPath && processPath.length > 0) {
            renderQueue.push(() => {
                try {
                    renderDepartmentOverviewTable(processPath);
                } catch (e) {
                    console.error('❌ Error rendering department overview:', e);
                }
            });
            renderQueue.push(() => {
                try {
                    renderProcessPathTable(processPath);
                } catch (e) {
                    console.error('❌ Error rendering process path tables:', e);
                }
            });
        } else {
            console.warn('⚠️ Process path data empty or failed');
        }

        // 2. Full-day PPR totals for footer
        if (fullDayPPR) {
            renderQueue.push(() => {
                try {
                    fullDayPPRTotals = fullDayPPR;
                    updateProcessPathFooterTotals(fullDayPPRTotals);
                } catch (e) {
                    console.error('❌ Error updating PPR footer totals:', e);
                }
            });
        }

        // 3. Hourly Breakdown - Support Hours
        if (hourly && hourly.length > 0) {
            renderQueue.push(() => {
                try {
                    renderHourlyTable(hourly);
                } catch (e) {
                    console.error('❌ Error rendering hourly table:', e);
                }
            });
        } else {
            console.warn('⚠️ Hourly data empty or failed');
        }

        // 4. Summary - Support Hours
        if (summary) {
            renderQueue.push(() => {
                try {
                    renderSummaryTable(summary);
                } catch (e) {
                    console.error('❌ Error rendering summary table:', e);
                }
            });
        }

        // 5. Time Off Task (at the bottom)
        if (timeOffTask) {
            renderQueue.push(() => {
                try {
                    renderTimeOffTaskTable(timeOffTask);
                } catch (e) {
                    console.error('❌ Error rendering time off task table:', e);
                }
            });
        }

        // Update loading bar text to show rendering phase
        const loadingText = document.querySelector('.loading-text');
        if (loadingText) {
            loadingText.textContent = 'Rendering tables...';
        }

        // Execute render queue with staggered animation frames
        // Keep loading bar visible until all renders complete and browser has painted
        function executeRenderQueue(index) {
            if (index >= renderQueue.length) {
                // All renders complete - wait for final paint before hiding loading bar
                requestAnimationFrame(() => {
                    requestAnimationFrame(() => {
                        // Double rAF ensures browser has painted everything
                        hideLoadingBar();
                        updateLastUpdateTime();
                        startCountdown();
                        startAutoRefresh();
                        document.dispatchEvent(new CustomEvent('refreshComplete'));
                        if (failedSlots.length > 0) {
                            showErrorNotification();
                        }
                    });
                });
                return;
            }
            
            requestAnimationFrame(() => {
                renderQueue[index]();
                // Small timeout to let browser paint before next render
                setTimeout(() => executeRenderQueue(index + 1), 5);
            });
        }
        
        executeRenderQueue(0);
        } finally {
            isFetching = false;
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    function init() {
        const refreshBtn = document.getElementById('refreshBtn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', fetchAllData);
        }

        document.addEventListener('renderTargets', renderTargetSettings);

        document.addEventListener('refreshTablesWithTargets', () => {
            renderHourlyTable();
            renderProcessPathTable();
        });

        const autoRefreshToggle = document.getElementById('autoRefreshToggle');
        if (autoRefreshToggle) {
            updateSwitchText();

            autoRefreshToggle.addEventListener('change', () => {
                updateSwitchText();

                if (autoRefreshToggle.checked) {
                    startCountdown();
                    startAutoRefresh();
                } else {
                    if (autoRefreshTimer) {
                        clearTimeout(autoRefreshTimer);
                        autoRefreshTimer = null;
                    }
                    if (countdownInterval) {
                        clearInterval(countdownInterval);
                    }
                    const nextRefreshEl = document.getElementById('nextRefreshTime');
                    if (nextRefreshEl) {
                        nextRefreshEl.textContent = '--:--';
                    }
                }
            });
        } else {
            console.warn('⚠️ autoRefreshToggle element not found in DOM');
        }

        const refreshIntervalSelect = document.getElementById('refreshInterval');
        if (refreshIntervalSelect) {
            refreshIntervalSelect.addEventListener('change', () => {
                const autoRefreshToggle = document.getElementById('autoRefreshToggle');
                if (autoRefreshToggle && autoRefreshToggle.checked) {
                    if (lastUpdateTime) {
                        startCountdown();
                        startAutoRefresh();
                    }
                }
            });
        } else {
            console.warn('⚠️ refreshInterval element not found in DOM');
        }

        loadTargets();
        
        // Initialize Function Rollup modal
        initFunctionRollupModal();

        // Handle discover functions request from HTML
        document.addEventListener('discoverFunctionsRequest', async function(e) {
            const { processIds, fc, monthPeriod } = e.detail;
            
            try {
                const results = {};
                
                // Build URLs for selected month (current or last)
                const now = new Date();
                const targetMonth = monthPeriod === 'current'
                    ? new Date(now.getFullYear(), now.getMonth(), 1)
                    : new Date(now.getFullYear(), now.getMonth() - 1, 1);
                const monthStr = `${targetMonth.getFullYear()}/${String(targetMonth.getMonth() + 1).padStart(2, '0')}/01`;
                
                const processNames = {
                    admin: 'Admin/HR/IT',
                    facilities: 'Facilities',
                    nonfc: 'Non FC Controllable'
                };
                
                // Fetch all 3 processes in parallel
                const fetchPromises = Object.entries(processIds).map(async ([processKey, processId]) => {
                    const url = `https://fclm-portal.amazon.com/reports/functionRollup?reportFormat=HTML&warehouseId=${fc}&processId=${processId}&spanType=Month&startDateMonth=${encodeURIComponent(monthStr)}&maxIntradayDays=1&startHourIntraday=0&startMinuteIntraday=0&endHourIntraday=0&endMinuteIntraday=0`;
                    
                    try {
                        const html = await fetchURL(url);
                        const functions = parseFunctionsFromFRUForDiscovery(html, processKey);
                        return { processKey, functions };
                    } catch (error) {
                        console.error(`Error fetching ${processNames[processKey]}:`, error);
                        return { processKey, functions: [] };
                    }
                });
                
                const fetchResults = await Promise.all(fetchPromises);
                fetchResults.forEach(({ processKey, functions }) => {
                    results[processKey] = functions;
                });
                
                // Send results back to HTML
                document.dispatchEvent(new CustomEvent('discoverFunctionsResult', {
                    detail: { functions: results }
                }));
                
            } catch (error) {
                console.error('Error discovering functions:', error);
                document.dispatchEvent(new CustomEvent('discoverFunctionsResult', {
                    detail: { error: error.message }
                }));
            }
        });
    }

})();
