const {database} = require('../server/connections.js')
const _ = require('lodash')


module.exports.findById = async id => {
    if (!id) return false
    const doc = await database.main.get(id)
    if (doc && doc.type === 'chat') return doc
    return false
}

module.exports.findByOwner = (ownerID) => new Promise ((resolve, reject) => {
    if (!ownerID) return false
    try {
        database.main.view('chat', 'forOwner', {
            keys: [ownerID],
            include_docs: false,
        }).then((result) => {
            resolve(result.rows ?? false)
        })
    } catch (error) {
        reject(error)
    }
})

module.exports.create = (body) => {
    const { name, type = 'text', visibility = '', owner } = body

    const timestamp = Date.now()

    const chat = {
        name,
        visibility,
        chat_type: type,
        type: 'chat',
        owner,
        members: [
            {
                _id: owner,
                role: 'owner',
            },
            {
                _id: 'emery',
                role: 'bot',
            }
        ],
        created: timestamp
    }

    return save(chat)
}

module.exports.sendMessage = async (user, body) => {
    const timestamp = Date.now()

    const message = {
        timestamp,
        user: user._id,
        content: body.content ?? '',
        attachments: body.attachments ?? []
    }
    const chat = await this.findById(body.chat_id)
    if (chat) {
        if (!chat.messages) {
            chat.messages = [message]
        } else {
            chat.messages.push(message)
        }

        await save(chat)
        return message

    } else {
        return false
    }
}

const save = (chat) => {

    const savedProps = {...chat}

    return new Promise ((resolve, reject) => {
        exports.findById(chat._id).then((doc) => {
            if (doc) chat._rev = doc._rev
            database.main.insert(chat).then((doc) => {
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