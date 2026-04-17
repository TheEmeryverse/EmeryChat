
const session = require('express-session')

module.exports = require('../connections').createStore().then(sessionStore => session({
    name: 'sessions',
    rolling: true,
    cookie: {
        httpOnly: true,
        secure: process.env.NODE_ENV !== 'dev',
        maxAge: 1000 * 60 * 60 * 24 * 7, // 1 week
        sameSite: 'lax'
    },
    resave: false,
    proxy: undefined,
    saveUninitialized: false,
    unset: 'destroy',
    store: sessionStore,
    secret: process.env.SESSION_SECRET
})) 
