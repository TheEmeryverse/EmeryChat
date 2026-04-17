const path = require('path')

process.env.file = '../.env'

require('dotenv').config({
    path: path.join(process.cwd(), process.env.file)
})

module.exports = process.env
