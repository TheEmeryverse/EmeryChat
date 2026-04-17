const {database} = require('../server/connections.js')
const bcrypt = require('bcryptjs')
const _ = require('lodash')

const findUserByName = (name) => new Promise ((resolve, reject) => {
    try {
        const name_query = name.toLowerCase()
        database.main.view('user', 'forName', {
            keys: [name_query],
            include_docs: true,
        }).then((result) => {
            resolve(result.rows[0]?.doc ?? false)
        })
    } catch (error) {
        reject(error)
    }
})

module.exports.findById = async id => {
    if (!id) return false
    const doc = await database.main.get(id)
    if (doc && doc.type === 'user') return doc
    return false
}
const findByIds = (ids = []) => new Promise ((resolve, reject) => {
    try {
        database.main.view('user', 'all', {
            keys: ids,
            include_docs: true,
        }).then((result) => {
            resolve(result.rows ?? false)
        })
    } catch (error) {
        reject(error)
    }
})

module.exports.findUserByName = findUserByName
module.exports.findByIds = findByIds

const checkPassword = (user, password) => {
    return bcrypt.compareSync(password, user.password)
}

module.exports.checkPassword = checkPassword

module.exports.loginAttempt = function loginAttempt(user, req, password) {
    if (!user.loginHistory) user.loginHistory = []
    valid = checkPassword(user, password)
    const timestamp = Date.now()
    user.loginHistory.push({
        valid,
        ts: timestamp
    })
    if (!valid && user.loginHistory.length > 3 && !user.loginHistory[user.loginHistory.length - 2].valid && (timestamp - user.loginHistory[user.loginHistory.length - 4].ts) < 300000) {
        user.locked = true
    } else if (valid) {
        user.locked = false
        user.lastLogin = timestamp
    }
    return save(user).then(() => valid)
}

module.exports.create = async (body) => {
    const {password, name} = body

    const salt = bcrypt.genSaltSync(10)
    const newPW = bcrypt.hashSync(password, salt)
    const timestamp = Date.now()

    const user = {
        name,
        password: newPW,
        type: 'user',
        lastLogin: timestamp,
        loginHistory: [
            {
                success: true,
                ts: timestamp
            }
        ]
    }

    return save(user)
}

const save = (user) => {
    const savedProps = {...user}

    return new Promise ((resolve, reject) => {
        exports.findById(user._id).then((doc) => {
            if (doc) user._rev = doc._rev
            database.main.insert(user).then((doc) => {
                savedProps._id = doc._id ?? doc.id
                savedProps._rev = doc._rev ?? doc.rev
                if (doc.id) delete doc.id
                if (doc.rev) delete doc.rev
                _.merge(doc, savedProps)
                resolve(doc)
            }).catch((error) => {
                reject(error)
            })
        })
    })
}

module.exports.save = save