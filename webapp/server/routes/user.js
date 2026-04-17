require('../../env.js')
const express = require('express')
const {setHeaders} = require('../connections.js')
const authentication = require('../middleware/authentication.js')
const {isValidUserName, isValidPassword} = require('../../common/helpers.js')
const User = require('../../models/user')
const router = express.Router()

router.get('/logout', (req, res) => {
    setHeaders(res)
    authentication.logout()
})

router.post('/login',
    authentication.login(),
    authentication.deserialize(),
    (req, res, next) => {
        setHeaders(res)

        return res.json({
            success: true,
            redirect: '/'
        })
})

router.post('/signup', async (req, res, next) => {
    setHeaders(res)
    let {
        password,
        name
    } = req.body

    if (!password || !name || !isValidUserName(name)) {
        return res.json({
            success: false,
            reason: "Missing Info"
        })
    }

    const passwordValidity = isValidPassword(password)

    if (!passwordValidity.isValid) {
        return res.json({
            success: false,
            reason: passwordValidity.errors[0]
        })
    }

    User.findUserByName(name).then(async (doc)=> {
        if (doc) {
            return res.json({
                success: false,
                reason: "User Exists"
            })
        }

        await User.create({password, name})

        next()
        
    }, (error) => {console.warn('Failed to Query User:', error)})
    },
    authentication.login(),
    authentication.deserialize(),
    authentication.required(),
    (req, res, next) => {
        res.json({
            success: true,
            redirect: '/'
        })
    }
)

router.get('/me', authentication.required(), (req, res, next) => {
    setHeaders(res)

    const channels = req.user.channels ?? []
    const {name, avatar = '', loginHistory = [], _id} = req.user
    const session = loginHistory[loginHistory.length - 1].sessionID

    return res.json({
        success: true,
        channels,
        name,
        avatar,
        session,
        _id,
    })
})

module.exports = router
