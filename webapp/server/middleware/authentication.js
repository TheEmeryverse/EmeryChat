require('../../env.js')
const passport = require('passport')
const User = require('../../models/user')

const passportSession = passport.session()

exports.authenticate = (strategies, options = {}) => (req, res, next) => {
    passport.authenticate(
        strategies,
        Object.assign(options, {
            authInfo: true,
            keepSessionInfo: true,
            failWithError: true,
            failureMessage: options?.session ?? true
        })
    )(req, res, error => {
        if (!error) return next()
        const session = req.session
        const messages = session && session.messages
        const message = messages && messages[messages.length - 1]

        if (messages) delete session.messages
        error.code = parseInt(error.status) || error.code
        error.message = message || error.message
        
        next(error)
    })
}

const isValidSession = req => {
    const {loginHistory = []} = req.user

    if (loginHistory.length) {
        const {valid, sessionID} = loginHistory[loginHistory.length - 1]
        return valid && sessionID === req.sessionID
    }
}

exports.required = (options = {}) => async (req, res, next) => {
    const {error} = options
    let messages = []
    if (req.isAuthenticated()) {
        if (isValidSession(req)) {
            return next()
        }
        req.logout(() => {})
        messages = ['Signed in on another device.']
    }
    
    if (!messages.length) {
        messages = ['Session expired']
    }

    req.session.regenerate(() => {
        req.session.messages = messages
        next(error || new Error('User not authenticated', {code: 401}))
    })
}

exports.login = options => exports.authenticate(['local-login'], options)

exports.deserialize = () => (req, res, next) => {
    passportSession(req, res, () => {
        req.session.save(error => {
            if (error) return next(error)

            const login = req.user.loginHistory.at(-1)
            if (login.valid && !login.sessionID) {
                login.sessionID = req.sessionID
                User.save(req.user).then(user => {
                    req.user._rev = user._rev
                    next()
                }).catch(next)
            } else {
                next (new Error('Invalid login'))
            }
        })
    })
}

exports.logout = () => (req, res, next) => {
    req.logout(error => {
        if (error) return next(error)
    })
    req.session.destroy()
    res.json({
        redirect: '/login'
    })
}
