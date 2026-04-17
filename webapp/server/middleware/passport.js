const util = require('util')
const passport = require('passport')
const LocalStrategy = require('passport-local').Strategy
const User = require('../../models/user')

function LoginStrategy() {
    LocalStrategy.call(this, {
        usernameField: 'name',
        passwordField: 'password',
        passReqToCallback: true,
    }, (req, name, password, done) => {
        User.findUserByName(name).then(async user => {
            const timestamp = Date.now()
            if (!user)
                done(null, false, {message: 'Invalid Credentials.'})
            else if (user.locked && (timestamp - user.loginHistory[user.loginHistory.length - 1].ts) < 300000) 
                done(null, false, {message: 'Account locked, please try again later.'})
            else {
                const valid = await User.loginAttempt(user, req, password)
                if (!valid)
                    done(null, false, {message: 'Invalid Credentials.'})
                else {
                    await User.save(user)
                    done(null, user._id)
                }
            }
        }).catch(done)
    })
    this.name = 'local-login'
}

util.inherits(LoginStrategy, LocalStrategy)
passport.use(new LoginStrategy())

passport.serializeUser((id, done) => {
    done(null, id)
})

passport.deserializeUser((id, done) => {
    User.findById(id)
        .then(user => {
            done(null, user || false)
        })
        .catch(done)
})

module.exports = passport
