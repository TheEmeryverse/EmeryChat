require('../env.js')
const cors = require('cors')
const passport = require('./middleware/passport.js')
const {connectDatabase, corsOptions, database} = require('./connections.js')
const authentication = require('./middleware/authentication.js')

connectDatabase()
while(!database.main) {}

const express = require('express')
const app = express()

const http = require('http').createServer(app)

app.disable("X-Powered-By")
app.set("trust proxy", 1)
app.use(cors(corsOptions))

app.use(function(req, res, next) {
    res.header("Access-Control-Allow-Credentials", true);
    res.header("Access-Control-Allow-Origin", corsOptions.origin);
    res.header("Access-Control-Allow-Headers",
    "Origin, X-Requested-With, Content-Type, Accept, Authorization, X-HTTP-Method-Override, Set-Cookie, Cookie");
    res.header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE");
    next();  
})

app.use(express.json())

if (process.env.NODE_ENV === 'prod') {
    app.set('trust proxy', 1)
}

require('./middleware/session.js').then(session => {

    app.use(session)
    app.use(passport.initialize())
    app.use(passport.session())
    
    app.use('/user', require('./routes/user.js'))

    app.use(authentication.required())

    app.use('/api', require('./routes/api.js'))
    
    
    http.listen(3000, () => console.log('Server running on port 3000'))
})
