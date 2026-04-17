require('../env.js')
const {RedisStore} = require('connect-redis')

const disableTTL = process.env.NODE_ENV === 'dev'

const createStore = async () => {
    const client = require('redis').createClient({url: 'redis://127.0.0.1:6379'})
    await client.connect()
    const redisStore = new RedisStore({
        client,
        prefix: '',
        disableTTL
    })
    redisStore.disconnect ??= () => client.destroy()
    return redisStore
}

const nano = require('nano')
let database = {
    main: void 0
}

function connectDatabase() {
    try {
        database.main = nano(`${process.env.COUCHDB_PROTOCOL}://${process.env.COUCHDB_USERNAME}:${process.env.COUCHDB_PASSWORD}@${process.env.COUCHDB_ADDRESS}:${process.env.COUCHDB_PORT}`).use(process.env.COUCHDB_NAME)
    } catch (error) {
        console.error('Could not connect to Database:', error)
    }
}

const setHeaders = (res) => {
    res.setHeader('Access-Control-Allow-Origin', 'http://localhost:4000')
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT')
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type')
}

const corsOptions = {
    origin: 'http://localhost:4000',
    credentials: true,
    methods: "GET, POST, PUT, DELETE",
    optionsSuccessStatus: 200 // some legacy browsers (IE11, various SmartTVs) choke on 204
}

module.exports = {
    setHeaders,
    corsOptions,
    database,
    connectDatabase,
    createStore
}
