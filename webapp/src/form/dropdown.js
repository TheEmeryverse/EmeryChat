import * as React from 'react'

export const Dropdown = (props) => {
    const { options = [], onChange = () => {}, name = 'dropdown' } = props

    return <select
                name="dropdown"
                className='dropdownMenu'
                onChange={onChange}
            >
        {
            options.map(option => {
                const {value, label} = option
                return <option key={value} value={value}>{label}</option>
            })
        }
    </select>
}