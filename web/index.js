// Вход для `node --test web/`: Node 22 не раскрывает каталог сам, поэтому подключаем все web/*.test.js.
'use strict';
const fs = require('node:fs');
const path = require('node:path');

fs.readdirSync(__dirname).filter((f) => f.endsWith('.test.js')).sort().forEach((f) => require(path.join(__dirname, f)));
