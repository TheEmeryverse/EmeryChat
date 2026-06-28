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
    const {name, avatar = '', loginHistory = [], _id, configurations = {}} = req.user
    const session = loginHistory[loginHistory.length - 1].sessionID

    return res.json({
        success: true,
        channels,
        name,
        avatar,
        session,
        configurations,
        _id,
    })
})
router.post('/configurations', authentication.required(), async (req, res, next) => {
    setHeaders(res)

    const configurations = req.body
    if (!configurations || typeof configurations !== 'object') {
        return res.json({
            success: false,
            reason: "Invalid body: expected an object of configuration key/value pairs"
         })
    }

    try {
        const user = req.user
        user.configurations = {
            ...user.configurations,
            ...configurations
        }

        await User.save(user)

        return res.json({
            success: true,
            configurations: user.configurations
         })
    } catch (error) {
        console.warn('Failed to save user configurations:', error)
        return res.json({
            success: false,
            reason: "Failed to save configurations"
         })
    }
})


module.exports = router
