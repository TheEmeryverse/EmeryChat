require('../env.js')

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
}
