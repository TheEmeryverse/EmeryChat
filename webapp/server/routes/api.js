require('../../env.js')
const express = require('express')
const {setHeaders} = require('../connections.js')
const router = express.Router()

router.get('/ping', (req, res) => {
    setHeaders(res)
    res.json({
        status: 'running',
        time: Date.now()
    })
})

module.exports = router
