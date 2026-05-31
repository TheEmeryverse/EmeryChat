require('../../env.js')
const express = require('express')
const _ = require('lodash')
const {setHeaders} = require('../connections.js')
const Chat = require('../../models/chat.js')
const User = require('../../models/user.js')
const router = express.Router()

router.post('/create', async (req, res, next) => {
    setHeaders(res)

    const { name = 'New Chat', visibility = 'admin', type = 'text', owner} = req.body
    let chat_doc

    try {
        chat_doc = await Chat.create({
            name,
            visibility,
            owner,
            type
        })

        return res.json({
            success: true,
            redirect: chat_doc._id,
        })

    } catch (error) {
        return res.json({
            success: false,
            reason: 'Server error'
        })
    }
})

router.post('/message', async (req, res, next) => {
    setHeaders(res)

    try {
        await Chat.sendMessage(req.user, req.body)
        return res.json({
            success: true,
        })
    } catch (error) {
        console.log(error)
        return res.json({
            success: false,
            reason: 'Server error'
        })
    }
    
})

router.get('/forUser', async (req, res, next) => {
    setHeaders(res)

    const id = req.user._id
    try {
        let chat_data = await Chat.findByOwner(id)

        if (!chat_data || !chat_data.length) {
            chat_data = await Chat.create({
                name: 'Emery Chat',
                visibility: 'admin',
                type: 'text',
                owner: id
            })
        }

        chat_data = chat_data[0].value

        const ids = chat_data.members.map(member => member._id)
        const members_list = await User.findByIds(ids)
        const members_data = members_list.map(member_data => {
            const this_member_index = chat_data.members.findIndex((member) => member._id === member_data.id)
            return {
                ...member_data.value,
                role: chat_data.members[this_member_index].role,
                avatar: member_data.doc.avatar ?? false
            }
        })
        members_data.push({
            _id: 'emery',
            name: 'Emery Chat',
            role: 'bot',
            avatar: false
        })
        chat_data.members = members_data

        const member_details = {}
        members_data.forEach(member => {
            member_details[member._id] = member
        })

        chat_data.memberDetails = member_details

        return res.json({
            chat_data,
            success: true
        })

    } catch(e) {
        return res.json({
            success: false,
            reason: 'server error'
        })
    }
})

router.get('/:id', async (req, res, next) => {
    setHeaders(res)

    const { id } = req.params

    if (!id) {
        return res.json({
            success: false,
            reason: 'no id'
        })
    }

    try {
        const chat_data = await Chat.findById(id)

        const ids = chat_data.members.map(member => member._id)
        const members_list = await User.findByIds(ids)
        const members_data = members_list.map(member_data => {
            const this_member_index = chat_data.members.findIndex((member) => member._id === member_data.id)
            return {
                ...member_data.value,
                role: chat_data.members[this_member_index].role,
                avatar: member_data.doc.avatar ?? false
            }
        })
        members_data.push({
            _id: 'emery',
            name: 'Emery Chat',
            role: 'bot',
            avatar: false
        })
        chat_data.members = members_data

        const member_details = {}
        members_data.forEach(member => {
            member_details[member._id] = member
        })
        
        chat_data.memberDetails = member_details

        return res.json({
            success: true,
            chat_data,
        })

    } catch (e) {
        return res.json({
            success: false,
            reason: 'server error'
        })
    }
})

module.exports = router
